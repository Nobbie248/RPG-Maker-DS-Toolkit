"""Core ROM, translation, project, and CHBG support for RPG Tsukuru DS/DS+."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import struct
import textwrap
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageOps

import ndspy.code
import ndspy.codeCompression
import ndspy.rom

from rpgds_text import (
    format_tokens,
    has_unsafe_control_chars,
    referenced_strings,
    referenced_strings_in_data,
)


PROJECT_VERSION = 1
CHBG_SIZE_ALLOWANCE_PERCENT = 50
STRUCTURAL_ASSET_SUFFIX_RE = re.compile(r"(?P<suffix>:\d{3})$")

# Verified CP932 prose/UI pools in the decompressed ARM9. Other pointer targets
# include keyboard layouts, character conversion tables, and ARM instructions
# that happen to decode as Japanese; treating those as translations corrupts
# text input or executable code.
DS_ARM9_TEXT_RANGES = (
    (0xB3F08, 0xB3FF0),
    (0xB43B0, 0xB58A0),
    (0xB8638, 0xB868C),
    (0xB900C, 0xBC270),
    (0xCEE48, 0xCFF00),
)

DS_PLUS_ARM9_TEXT_RANGES = (
    (0xAEC4C, 0xAED40),
    (0xAF138, 0xB0360),
    (0xB2B68, 0xB2F50),
    (0xB3100, 0xB6000),
    (0xC8C04, 0xC9DF0),
    (0xCE3C4, 0xCE890),
)
ARM9_CODE_SETTINGS_MAGIC = b"\x21\x06\xC0\xDE\xDE\xC0\x06\x21"


@dataclass(frozen=True)
class ROMProfile:
    key: str
    title: str
    game_code: bytes
    sha256: str
    arm9_text_ranges: tuple[tuple[int, int], ...]
    output_name: str
    excluded_text_keys: frozenset[str] = frozenset()
    extra_arm9_offsets: tuple[int, ...] = ()
    excluded_overlay_ranges: tuple[tuple[int, int, int], ...] = ()
    allow_text_relocation: bool = True
    fixed_slot_overlays: frozenset[int] = frozenset()


ROM_PROFILES = {
    b"V29J": ROMProfile(
        "ds", "RPG Tsukuru DS", b"V29J",
        "5E845B09DA14C8CE80D50ACCFB1EBC6A350F4A4A5CE1DB1D6CF8439416F9D7CF",
        DS_ARM9_TEXT_RANGES, "RPG Tsukuru DS (English).nds",
    ),
    b"VEBJ": ROMProfile(
        "dsplus", "RPG Tsukuru DS+ - Create the New World", b"VEBJ",
        "D1FF98FE4FDE406B004D3C45986216F9EB67D3C765F1B2F16213677E005E216F",
        DS_PLUS_ARM9_TEXT_RANGES, "RPG Tsukuru DS+ (English).nds",
        frozenset({
            "1:67D", "1:C41D", "1:24791", "1:3883D",
            "1:33631",
            "2:DBB1", "2:123CD", "2:1B6E0",
            "5:14C60", "5:4D950", "5:687E4", "5:761AC",
            "5:4CD8C", "5:4FE50", "5:4FFF0",
            "11:1BE3C", "11:1BEA8", "11:1BF10", "11:1BF78", "11:1BFE0",
            "13:2C9D", "14:22D0", "16:22D64",
            "10:1C24", "10:4EF0",
            # Character-entry punctuation and kana index tables, not UI text.
            "16:220D8", "16:220EC", "16:22100", "16:221AC", "16:221C8",
            "16:2228C", "16:222E4", "16:2246C", "16:224D8",
        }),
        (0xB541C, 0xB5448, 0xB5474, 0xB54A0),
        # Overlay 16 stores kana groups, punctuation, and character-conversion
        # lookup tables here. They are pointer-referenced CP932 data, but are
        # not display strings and must remain byte-for-byte original.
        ((16, 0x00000, 0x230D0),),
    ),
}

NON_RELOCATABLE_KEYS = frozenset({
    "-1:B53F0", "-1:B541C", "-1:B5448", "-1:B5474", "-1:B54A0",
    # These three identical acquisition messages sit in an overlay 5 runtime
    # table used by treasure/event playback. Keep the table entries at their
    # original addresses even though each translated payload fits its slot.
    "5:7D6E8", "5:7D6FC", "5:7D710",
    # DS+ easy-event message templates are copied into serialized event data.
    # Keep them within their original byte budgets so the generated command
    # offsets and following fields retain the layout expected by overlay 5.
    "9:51B4D", "9:51BD2", "9:51C89", "9:51CC8", "9:51CE9", "9:51D0A",
    # This suffix is appended to an item/money name and fed to a legacy
    # two-byte-at-a-time serializer.  An odd CP932 byte count makes that
    # routine step over the NUL terminator and overwrite its DTCM stack.
    "9:5264C",
})

PAIR_SERIALIZED_TEXT_FALLBACKS = {
    # Easy-event messages are converted through a private compact character
    # set before being stored in generated event data.  Plain ASCII does not
    # round-trip through its inverse table; use short full-width source text
    # so playback reconstructs the intended Latin glyphs.
    "9:51B3C": "SAVE?",
    "9:51BD2": "CHEST EMPTY",
    "9:51B5E": "WELCOME",
    "9:51C1B": "THANK YOU!",
    "9:51B6F": "AN INN",
    "9:51B80": "NITE %dG",
    "9:51BBB": "STAY NIGHT?",
    "9:51B91": "REST WELL",
    "9:51CC8": "COME AGAIN!",
    "9:51CE9": "NOT ENOUGH GOLD",
    "9:51B4D": "CHURCH",
    "9:51C02": "REVIVE KO",
    "9:51C89": "I SHALL PRAY.",
    "9:51BA6": "FEE %dG",
    "9:51D74": "MAY I PRAY?",
    "9:51BE9": "WOUNDED ONE",
    "9:51C34": "AWAKEN SOUL",
    "9:51B1C": "ARISE!",
    "9:51C6C": "GOD BLESS YOU",
    "9:51D0A": "NOT ENOUGH GOLD",
    "9:5264C": " FOUND!",
}

PAIR_SERIALIZED_TEXT_KEYS = frozenset(PAIR_SERIALIZED_TEXT_FALLBACKS)
PAIR_SERIALIZED_PREFIX_KEYS = frozenset({"9:5264C"})

# The easy-event/template block may have layout-sensitive consumers that are
# not visible in the direct-pointer index. Conservatively keep the whole block
# out of the donor pool; every current translation here fits its original slot.
NON_RELOCATABLE_RANGES = (
    (9, 0x51A80, 0x51E20),
)


def profile_for_rom(rom: ndspy.rom.NintendoDSRom) -> ROMProfile:
    profile = ROM_PROFILES.get(bytes(rom.idCode))
    if profile is None:
        supported = ", ".join(code.decode("ascii") for code in ROM_PROFILES)
        raise ValueError(f"Unsupported ROM game code {bytes(rom.idCode)!r}; expected {supported}")
    return profile


def _compress_arm9(data: bytes, ram_address: int) -> bytearray:
    """Compress ARM9 code and update the SDK decompressor's end pointer."""
    compressed = bytearray(ndspy.codeCompression.compress(data, isArm9=True))
    magic_offset = compressed.find(ARM9_CODE_SETTINGS_MAGIC, 0, min(len(compressed), 0x8000))
    code_settings_offset = magic_offset - 0x1C
    if magic_offset < 0 or code_settings_offset < 0 or code_settings_offset + 0x18 > len(compressed):
        raise ValueError("Could not locate ARM9 module parameters after compression")
    struct.pack_into("<I", compressed, code_settings_offset + 0x14,
                     ram_address + len(compressed))
    return compressed


def structural_asset_suffix(text: str) -> str:
    """Return a trailing catalog asset ID such as ``:001``, if present.

    DS+ stores display labels and resource IDs in the same NUL-terminated
    string.  Overlay 7 finds the colon at runtime and parses the three digits
    after it to build paths such as ``town/town-house001.ebbm``.  The suffix is
    therefore control data, not translatable punctuation.
    """
    match = STRUCTURAL_ASSET_SUFFIX_RE.search(text)
    return match.group("suffix") if match else ""


def _structural_label_prefix(translation: str, suffix: str) -> str:
    """Remove a translated/duplicated terminal asset ID from a label."""
    value = normalize_english(translation).rstrip()
    asset_id = suffix[1:]
    unpadded_asset_id = str(int(asset_id))

    # Handle a normal, spaced, or incorrectly translated colon suffix first.
    colon_tail = re.search(r"\s*:\s*\d{3}\s*$", value)
    if colon_tail:
        value = value[:colon_tail.start()].rstrip()
    elif re.search(r"\d{3}\s*$", value):
        # Online shortening previously produced forms such as
        # ``SnglHsSmll01001``.  The last three digits are the hidden asset ID;
        # any preceding digits remain part of the visible label.
        value = re.sub(r"\d{3}\s*$", "", value).rstrip(" :")
    elif (unpadded_asset_id != asset_id
          and re.search(rf"(?<!\d){re.escape(unpadded_asset_id)}\s*$", value)):
        # A few online results dropped the leading zero as well as the colon,
        # for example ``SHOP NODOOR84`` for the authoritative ``:084``.
        value = re.sub(
            rf"(?<!\d){re.escape(unpadded_asset_id)}\s*$", "", value
        ).rstrip(" :")

    # Collapse a duplicate exact suffix (for example ``House:001001``).
    exact_tail = re.compile(rf"\s*:\s*{re.escape(asset_id)}\s*$")
    while exact_tail.search(value):
        value = exact_tail.sub("", value).rstrip()
    # The loader parses the first colon, so no translated punctuation may
    # precede the single authoritative suffix.
    value = re.sub(r"\s+", " ", value.replace(":", " "))
    return value.rstrip(" :")


def structural_suffix_is_preserved(original: str, translation: str) -> bool:
    suffix = structural_asset_suffix(original)
    return not suffix or translation.endswith(suffix)


def translation_fits(original: str, translation: str, max_bytes: int) -> bool:
    """Return whether a manual translation can safely occupy a fixed text slot."""
    if not translation or has_unsafe_control_chars(translation):
        return False
    try:
        used_bytes = len(translation.encode("cp932"))
    except UnicodeEncodeError:
        return False
    return (used_bytes <= max_bytes
            and format_tokens(original) == format_tokens(translation)
            and structural_suffix_is_preserved(original, translation))


def translation_is_safe(original: str, translation: str) -> bool:
    """Return whether text is encodable and preserves the game's format tokens.

    Unlike ``translation_fits``, this permits text that needs relocation into
    the owning ARM9/overlay's existing text storage.
    """
    if not translation or has_unsafe_control_chars(translation):
        return False
    try:
        translation.encode("cp932")
    except UnicodeEncodeError:
        return False
    return (format_tokens(original) == format_tokens(translation)
            and structural_suffix_is_preserved(original, translation))


@dataclass
class TextEntry:
    overlay: int
    offset: int
    address: int
    max_bytes: int
    original: str
    translation: str = ""
    auto: bool = False

    @property
    def key(self) -> str:
        return f"{self.overlay}:{self.offset:X}"

    @property
    def used_bytes(self) -> int:
        try:
            return len(self.translation.encode("cp932"))
        except UnicodeEncodeError:
            return -1

    @property
    def valid(self) -> bool:
        return entry_translation_is_safe(self)


@dataclass
class ImageAsset:
    file_id: int
    name: str
    width: int
    height: int
    bpp: int
    colors: int
    tile_count: int
    decompressed_size: int
    compressed: bool


@dataclass
class CHBGLayout:
    header: bytes
    width: int
    height: int
    bpp: int
    colors: int
    tile_count: int
    palette: list[tuple[int, int, int]]
    tile_map: list[int]
    tile_data: bytes
    compressed: bool
    palette_data: bytes


@dataclass(frozen=True)
class CHBGEncodeResult:
    data: bytes
    required_tiles: int
    original_tiles: int
    capacity_tiles: int
    output_tiles: int
    original_decompressed_size: int
    output_decompressed_size: int
    palette_adjusted_pixels: int


class CHBGCapacityError(ValueError):
    """Raised when exact rendered pixels cannot fit the allowed tile budget."""

    def __init__(self, required_tiles: int, capacity_tiles: int,
                 original_tiles: int | None = None):
        self.required_tiles = required_tiles
        self.capacity_tiles = capacity_tiles
        self.original_tiles = original_tiles if original_tiles is not None else capacity_tiles
        super().__init__(
            f"Keeping every converted DS-palette pixel requires {required_tiles} "
            "distinct 8x8 tiles, "
            f"but the original asset's {CHBG_SIZE_ALLOWANCE_PERCENT}% decoded-data "
            f"allowance has room for only {capacity_tiles} "
            f"(originally {self.original_tiles}). "
            "PNG metadata is not counted, and duplicate tile patterns have "
            "already been compacted. "
            "The image was not imported because enlarging this data can corrupt "
            "the game's RAM/VRAM."
        )


EXACT_TRANSLATIONS = {
    "はい": "YES", "いいえ": "NO", "セーブ": "SAVE", "ロード": "LOAD",
    "閉じる": "CLOSE", "戻る": "BACK", "やめる": "QUIT", "キャンセル": "CANCEL",
    "決定": "OK", "確認": "CONFIRM", "次へ": "NEXT", "前へ": "PREV",
    "編集": "EDIT", "エディット": "EDIT", "削除": "DELETE", "消す": "DELETE",
    "コピー": "COPY", "貼り付け": "PASTE", "新規": "NEW", "作成": "CREATE",
    "すべて": "ALL", "なし": "NONE", "通常": "NORMAL", "種類": "TYPE",
    "フリー": "FREE", "内": "IN", "外": "OUT", "オン": "ON", "オフ": "OFF",
    "設定": "SETTINGS", "変更": "CHANGE", "選択": "SELECT", "入力": "INPUT",
    "見る": "VIEW", "プレビュー": "PREVIEW", "カメラ": "CAMERA",
    "ゲーム": "GAME", "ゲームデータ": "GAME DATA", "マップ": "MAP",
    "マップ名なし": "NO MAP NAME", "ダンジョン": "DUNGEON",
    "ダンジョン名": "DUNGEON NAME", "主人公": "HERO", "モンスター": "MONSTER",
    "アイテム": "ITEM", "武器": "WEAPON", "防具": "ARMOR", "特技": "SKILL",
    "戦闘": "BATTLE", "攻撃": "ATTACK", "防御": "DEFEND", "逃げる": "RUN",
    "魔法": "MAGIC", "レベル": "LEVEL", "経験値": "EXP", "お金": "MONEY",
    "グラフィック": "GRAPHICS", "イラスト画像": "ILLUSTRATION",
    "キャラクター": "CHARACTER", "アニメ": "ANIM", "アニメなし": "NO ANIM",
    "再生": "PLAY", "停止": "STOP", "試聴": "PREVIEW", "ピッチ": "PITCH",
    "効果音": "SFX", "ＢＧＭ": "BGM", "通信": "LINK", "送信": "SEND",
    "受信": "RECEIVE", "通信エラー": "LINK ERROR", "しばらくお待ちください": "PLEASE WAIT",
    "フレンドリスト": "FRIEND LIST", "フレンドコード": "FRIEND CODE",
    "タイトル": "TITLE", "タイトルへ戻る": "RETURN TO TITLE",
    "スタート": "START", "ゲームオーバー": "GAME OVER", "セーブ画面": "SAVE SCREEN",
    "デバッグ": "DEBUG", "マニュアル": "MANUAL", "デザイン": "DESIGN",
    "営業": "SALES", "広報": "PUBLICITY", "対象：": "TARGET:",
    "使用メモリ": "MEMORY USED", "残り": "LEFT", "空きデータがありません!!": "NO FREE SLOT!",
    "名前": "NAME", "説明": "DESCRIPTION", "作品名：": "TITLE:", "作者名：": "AUTHOR:",
    "プレイ時間": "PLAY TIME", "最終更新": "LAST UPDATE", "制作時間": "BUILD TIME",
    "送信する": "SEND", "手に入れる": "GET", "宝物庫": "TREASURY",
    "評価順": "BY RATING", "人気順": "BY POPULARITY", "古い順": "OLDEST",
    "新しい順": "NEWEST", "特別賞": "SPECIAL PRIZE", "最優秀賞": "TOP PRIZE",
    "街": "TOWN", "都市": "CITY", "城・塔": "CASTLE/TOWER", "村": "VILLAGE",
    "フィールド": "FIELD", "森": "FOREST", "雪": "SNOW", "土": "EARTH",
    "氷": "ICE", "鉱山": "MINE", "地下道": "TUNNEL", "荒れ地": "WASTELAND",
    "フィールド": "FIELD", "ダンジョン": "DUNGEON", "パーティ設定": "PARTY SETUP",
    "職業": "JOB", "ユニット": "UNIT", "出現範囲": "RANGE", "イベント": "EVENT",
    "オプション": "OPTIONS", "データロード": "DATA LOAD", "データの送受信": "DATA LINK",
    "画像ホルダー": "IMAGE HOLDER",
    "新しく作る": "CREATE NEW", "ゲームプレイ": "PLAY GAME", "データベース": "DATABASE",
    "作成メニュー": "CREATE MENU",
    "%sは動けない": "%s CANT MOVE",
    "武器なし": "NO WPN", "武器名なし": "NO WPN", "鎧なし": "NO ARM",
    "防具名なし": "NO ARM", "職業名なし": "NO JOB", "特技なし": "NO SKILL",
    "特技名なし": "NO SKILL", "説明なし": "NO DESC", "月無し": "NO-MON",
    "女神のﾚﾘｰﾌ": "GOD RELIC", "消耗しない": "NO-CNSM",
    "お店で売れないようにします。": "CANT SELL IN SHOPS",
    "何回使ってもなくならないアイテムにします。": "ITEM NEVER USED UP",
    "毎ターンごとに50％の確率で実行します。": "50% CHANCE EACH TURN",
    "毎ターンごとに20％の確率で実行します。": "20% CHANCE EACH TURN",
    "毎ターンごとに10％の確率で実行します。": "10% CHANCE EACH TURN",
    "水路08 水なし": "WTR08-NO", "水路07 水なし": "WTR07-NO",
    "水路11 水なし 神殿": "WTR11-NO-TMPL", "水路12 水なし 下水道": "WTR12-NO-SEWER",
    "水路09 水なし 小部屋": "WTR09-NO-SML", "水路10 水なし 中部屋": "WTR10-NO-MID",
    "戦闘不能の方を復活させる": "REVIVE KO", "戦闘不能にする": "CAUSE KO",
    "「宝物庫」ヘルプ未設定": "TREASURY HELP UNSET",
}

ABBREVIATIONS = {
    "please": "", "select": "choose", "character": "char", "characters": "chars",
    "configuration": "config", "information": "info", "description": "desc",
    "graphics": "gfx", "graphic": "gfx", "animation": "anim", "communication": "link",
    "destination": "dest", "maximum": "max", "minimum": "min", "remaining": "left",
    "equipment": "equip", "defense": "def", "attack": "atk", "experience": "exp",
    "button": "btn", "number": "no.", "screen": "menu", "settings": "setup",
    "download": "get", "completed": "done", "complete": "done", "currently": "now",
    "position": "pos", "message": "msg", "amount": "amt", "quantity": "qty",
    "inventory": "items", "previous": "prev", "difficulty": "diff", "initialize": "reset",
    "initialization": "reset", "connection": "link", "successful": "ok", "successfully": "ok",
}

PHRASE_SHORTENINGS = (
    (r"\bare you sure (?:that )?you want to\b", ""),
    (r"\bwould you like to\b", ""),
    (r"\bdo you want to\b", ""),
    (r"\bplease select\b", "choose"),
    (r"\bplease choose\b", "choose"),
    (r"\bplease enter\b", "enter"),
    (r"\bplease wait\b", "wait"),
    (r"\bthere (?:is|are) no\b", "no"),
    (r"\bnot enough\b", "low"),
    (r"\bdoes not exist\b", "missing"),
    (r"\bcould not be found\b", "missing"),
    (r"\b(?:was|has been) successfully\b", ""),
    (r"\bsuccessfully completed\b", "done"),
    (r"\bunable to\b", "can't"),
    (r"\bcannot\b", "can't"),
    (r"\breturn to the\b", "back to"),
    (r"\breturn to\b", "back to"),
    (r"\bin order to\b", "to"),
    (r"\bnumber of\b", "no. of"),
)

REMOVABLE_WORDS = {
    "a", "an", "the", "your", "currently", "really", "now", "then", "that", "this",
    "successfully", "please", "available", "selected", "following",
}

AGGRESSIVE_FILLERS = REMOVABLE_WORDS | {
    "about", "away", "be", "been", "being", "by", "can", "could", "during", "for", "from",
    "how", "in", "into", "is", "it", "may", "of", "on", "should", "to", "useful", "was",
    "were", "will", "would", "you",
}

WORD_COMPACTIONS = {
    "escape": "run", "flee": "run", "retreat": "run", "weapon": "wpn", "weapons": "wpns",
    "sword": "swd", "armor": "armr", "gauntlet": "glve", "explosion": "blast",
    "thunder": "thndr", "lightning": "ltng", "japanese": "katana", "paralysis": "para",
    "resuscitation": "revive", "absorption": "absorb", "medicinal": "med", "everyday": "daily",
    "feather": "fthr", "powder": "pwdr", "whistle": "whst", "mirror": "mirr",
    "cannot": "NO", "can't": "NO", "unable": "NO", "never": "NO", "not": "NO",
    "without": "NO",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def extract_text_entries(rom: ndspy.rom.NintendoDSRom) -> list[TextEntry]:
    profile = profile_for_rom(rom)
    entries: list[TextEntry] = []
    arm9_data = ndspy.codeCompression.decompress(bytes(rom.arm9))
    for offset, address, raw, original in referenced_strings_in_data(arm9_data, rom.arm9RamAddress):
        if any(start <= offset < end for start, end in profile.arm9_text_ranges):
            entries.append(TextEntry(-1, offset, address, len(raw), original))
    existing_arm9_offsets = {entry.offset for entry in entries if entry.overlay == -1}
    for offset in profile.extra_arm9_offsets:
        if offset in existing_arm9_offsets:
            continue
        end = arm9_data.find(b"\0", offset, min(len(arm9_data), offset + 512))
        if end < 0:
            raise ValueError(f"Missing NUL for profile text at ARM9 0x{offset:X}")
        raw = arm9_data[offset:end]
        entries.append(TextEntry(-1, offset, rom.arm9RamAddress + offset,
                                 len(raw), raw.decode("cp932")))
    for overlay_id, overlay in sorted(rom.loadArm9Overlays().items()):
        for offset, address, raw, original in referenced_strings(overlay):
            entries.append(TextEntry(overlay_id, offset, address, len(raw), original))
    return [
        entry for entry in entries
        if entry.key not in profile.excluded_text_keys
        and not any(
            entry.overlay == overlay_id and start <= entry.offset < end
            for overlay_id, start, end in profile.excluded_overlay_ranges
        )
    ]


def list_image_assets(rom: ndspy.rom.NintendoDSRom) -> list[ImageAsset]:
    assets: list[ImageAsset] = []
    for file_id, raw in enumerate(rom.files):
        data = bytes(raw)
        if not data.startswith(b"CHBG"):
            continue
        name = rom.filenames.filenameOf(file_id)
        layout = parse_chbg(data, name.lower().endswith(".blz"))
        decompressed_size = (
            16 + layout.colors * 2 + len(layout.tile_map) * 2
            + layout.tile_count * layout.bpp * 8
        )
        assets.append(ImageAsset(file_id, name, layout.width, layout.height, layout.bpp,
                                 layout.colors, layout.tile_count, decompressed_size,
                                 layout.compressed))
    return assets


def _bgr555_to_rgb(value: int) -> tuple[int, int, int]:
    return ((value & 31) * 255 // 31, ((value >> 5) & 31) * 255 // 31,
            ((value >> 10) & 31) * 255 // 31)


def _rgb_to_bgr555(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return ((r * 31 + 127) // 255) | (((g * 31 + 127) // 255) << 5) | (((b * 31 + 127) // 255) << 10)


def parse_chbg(raw: bytes, compressed: bool | None = None) -> CHBGLayout:
    if not raw.startswith(b"CHBG"):
        raise ValueError("Not a CHBG image")
    if compressed is None:
        decompressed = ndspy.codeCompression.decompress(raw)
        compressed = decompressed != raw
    elif compressed:
        decompressed = ndspy.codeCompression.decompress(raw)
    else:
        decompressed = raw

    if len(decompressed) < 16:
        raise ValueError("Truncated CHBG header")
    width, height, fmt, tile_count = struct.unpack_from("<4H", decompressed, 4)
    bpp = fmt & 0xFF
    colors = (fmt >> 8) * 16
    if bpp not in (4, 8) or not colors:
        raise ValueError(f"Unsupported CHBG format 0x{fmt:04X}")
    map_count = (width // 8) * (height // 8)
    palette_size = colors * 2
    map_size = map_count * 2
    tile_size = bpp * 8
    expected = 16 + palette_size + map_size + tile_count * tile_size
    if len(decompressed) != expected:
        raise ValueError(f"CHBG size mismatch: expected {expected}, got {len(decompressed)}")

    palette_values = struct.unpack_from(f"<{colors}H", decompressed, 16)
    map_offset = 16 + palette_size
    tile_map = list(struct.unpack_from(f"<{map_count}H", decompressed, map_offset))
    tile_data = decompressed[map_offset + map_size :]
    if tile_map and max(tile_map) >= tile_count:
        raise ValueError("CHBG tile map points beyond its tile data")
    return CHBGLayout(decompressed[:16], width, height, bpp, colors, tile_count,
                      [_bgr555_to_rgb(value) for value in palette_values], tile_map,
                      tile_data, bool(compressed), decompressed[16:map_offset])


def decode_chbg(raw: bytes, compressed: bool | None = None) -> Image.Image:
    layout = parse_chbg(raw, compressed)
    image = Image.new("RGB", (layout.width, layout.height))
    pixels = image.load()
    tiles_wide = layout.width // 8
    tile_bytes = layout.bpp * 8
    for map_index, tile_index in enumerate(layout.tile_map):
        tx, ty = map_index % tiles_wide, map_index // tiles_wide
        tile = layout.tile_data[tile_index * tile_bytes : (tile_index + 1) * tile_bytes]
        if layout.bpp == 8:
            indices = tile
        else:
            unpacked = bytearray()
            for value in tile:
                unpacked.extend((value & 0xF, value >> 4))
            indices = unpacked
        for py in range(8):
            for px in range(8):
                index = indices[py * 8 + px]
                pixels[tx * 8 + px, ty * 8 + py] = layout.palette[index]
    return image


def sanitize_import_image(image: Image.Image) -> Image.Image:
    """Return a detached, metadata-free 8-bit RGBA copy for CHBG encoding.

    EXIF orientation is applied before the tags are discarded. Creating the
    final image from raw pixel bytes ensures EXIF, XMP, IPTC, ICC, DPI,
    software, comments, and other PNG ancillary data cannot reach the encoder.
    """
    oriented = ImageOps.exif_transpose(image)
    rgba = oriented.convert("RGBA")
    return Image.frombytes("RGBA", rgba.size, rgba.tobytes())


def sanitize_png_bytes(png_data: bytes) -> bytes:
    """Strip PNG metadata and unnecessary opaque alpha without changing pixels."""
    with Image.open(io.BytesIO(png_data)) as source:
        clean = sanitize_import_image(source)
    if clean.getchannel("A").getextrema() == (255, 255):
        rgb = clean.convert("RGB")
        clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
    output = io.BytesIO()
    clean.save(output, "PNG", optimize=True, compress_level=9)
    return output.getvalue()


def _fit_chbg_tiles(layout: CHBGLayout, desired_tiles: list[bytes],
                    original_tiles: list[bytes],
                    capacity_tiles: int) -> tuple[list[bytes], list[int], int]:
    """Pack exact desired tiles within the decoded-data tile allowance.

    The common case retains each map cell's original tile ID. When one shared
    source tile splits into multiple edited appearances, new tiles are appended
    up to the allowed capacity first. Duplicate/unused primary appearances are
    reclaimed only if more IDs are needed. The rendered pixels stay exact while
    existing tile IDs remain as stable as possible.
    """
    required_order = list(dict.fromkeys(desired_tiles))
    required_tiles = len(required_order)
    if required_tiles > capacity_tiles:
        raise CHBGCapacityError(required_tiles, capacity_tiles, layout.tile_count)

    replacements: dict[int, Counter[bytes]] = {}
    for map_index, original_tile_id in enumerate(layout.tile_map):
        replacements.setdefault(original_tile_id, Counter())[desired_tiles[map_index]] += 1

    primary_tiles = list(original_tiles)
    for tile_id, choices in replacements.items():
        original_tile = original_tiles[tile_id]
        # If even one unchanged map cell still uses this tile, keep the
        # original payload at its original ID. Changed variants can use an
        # appended/reclaimed ID; this is safer for menu highlight code that
        # may refer to stable tile IDs outside the static map.
        primary_tiles[tile_id] = (
            original_tile if choices[original_tile]
            else choices.most_common(1)[0][0]
        )

    desired_set = set(required_order)
    ids_by_primary: dict[bytes, list[int]] = {}
    for tile_id, tile in enumerate(primary_tiles):
        ids_by_primary.setdefault(tile, []).append(tile_id)

    retention = Counter()
    for map_index, original_tile_id in enumerate(layout.tile_map):
        if desired_tiles[map_index] == primary_tiles[original_tile_id]:
            retention[original_tile_id] += 1

    # Keep at least one existing ID for every required primary appearance.
    reclaim_candidates: list[tuple[int, int, bool, int]] = []
    for tile, tile_ids in ids_by_primary.items():
        if tile in desired_set:
            keeper = max(
                tile_ids,
                key=lambda tile_id: (
                    retention[tile_id],
                    primary_tiles[tile_id] == original_tiles[tile_id],
                    -tile_id,
                ),
            )
            for tile_id in tile_ids:
                if tile_id != keeper:
                    reclaim_candidates.append((
                        1, retention[tile_id],
                        primary_tiles[tile_id] == original_tiles[tile_id], tile_id,
                    ))
        else:
            for tile_id in tile_ids:
                reclaim_candidates.append((
                    0, retention[tile_id],
                    primary_tiles[tile_id] == original_tiles[tile_id], tile_id,
                ))

    missing_tiles = [tile for tile in required_order if tile not in ids_by_primary]
    append_count = min(
        len(missing_tiles),
        max(0, capacity_tiles - layout.tile_count),
    )
    appended_tiles = missing_tiles[:append_count]
    reclaimed_tiles = missing_tiles[append_count:]
    reclaim_candidates.sort()
    reclaimed = [item[3] for item in reclaim_candidates[:len(reclaimed_tiles)]]
    if len(reclaimed) != len(reclaimed_tiles):
        raise CHBGCapacityError(required_tiles, capacity_tiles, layout.tile_count)

    tile_table = list(primary_tiles)
    tile_table.extend(appended_tiles)
    for tile_id, tile in zip(reclaimed, reclaimed_tiles):
        tile_table[tile_id] = tile

    reclaimed_set = set(reclaimed)
    ids_by_output: dict[bytes, list[int]] = {}
    for tile_id, tile in enumerate(tile_table):
        if tile_id not in reclaimed_set and tile in desired_set:
            ids_by_output.setdefault(tile, []).append(tile_id)
    for tile_id, tile in zip(reclaimed, reclaimed_tiles):
        ids_by_output.setdefault(tile, []).append(tile_id)

    preferred_id = {
        tile: max(ids, key=lambda tile_id: (retention[tile_id], -tile_id))
        for tile, ids in ids_by_output.items()
    }
    tile_map: list[int] = []
    for map_index, original_tile_id in enumerate(layout.tile_map):
        desired = desired_tiles[map_index]
        if (original_tile_id not in reclaimed_set
                and tile_table[original_tile_id] == desired):
            tile_map.append(original_tile_id)
        else:
            tile_map.append(preferred_id[desired])
    return tile_table, tile_map, required_tiles


def prepare_chbg_replacement(image: Image.Image, original_raw: bytes,
                             compressed: bool | None = None,
                             allow_global_exact_palette: bool = False) -> CHBGEncodeResult:
    layout = parse_chbg(original_raw, compressed)
    if image.size != (layout.width, layout.height):
        raise ValueError(f"Image must remain {layout.width}x{layout.height} pixels")

    # Keep palette indices stable. Some screens animate selections by referring
    # to particular palette entries and tile IDs, so rebuilding either table
    # can look correct at rest but display garbage when highlighted.
    # 4bpp graphics can only use the first 16 entries; 8bpp can use all 256.
    color_limit = min(layout.colors, 16 if layout.bpp == 4 else 256)
    palette = list(layout.palette)
    key_color = layout.palette[0]
    rgba_pixels = list(image.convert("RGBA").get_flattened_data())
    pixels: list[tuple[int, int, int]] = []
    for red, green, blue, alpha in rgba_pixels:
        if alpha == 0:
            pixels.append(key_color)
        elif alpha == 255:
            pixels.append((red, green, blue))
        else:
            inverse = 255 - alpha
            pixels.append((
                (red * alpha + key_color[0] * inverse + 127) // 255,
                (green * alpha + key_color[1] * inverse + 127) // 255,
                (blue * alpha + key_color[2] * inverse + 127) // 255,
            ))

    # Recover the original per-pixel indices. Unedited pixels keep their exact
    # original index, including duplicate palette colors with different roles.
    original_indices = bytearray(layout.width * layout.height)
    tiles_wide = layout.width // 8
    tile_bytes = layout.bpp * 8
    for map_index, tile_index in enumerate(layout.tile_map):
        tile = layout.tile_data[tile_index * tile_bytes : (tile_index + 1) * tile_bytes]
        if layout.bpp == 8:
            indices = tile
        else:
            unpacked = bytearray()
            for value in tile:
                unpacked.extend((value & 0xF, value >> 4))
            indices = unpacked
        tx, ty = map_index % tiles_wide, map_index // tiles_wide
        for py in range(8):
            destination = (ty * 8 + py) * layout.width + tx * 8
            original_indices[destination : destination + 8] = indices[py * 8 : py * 8 + 8]

    original_tiles = [
        bytes(layout.tile_data[index * tile_bytes : (index + 1) * tile_bytes])
        for index in range(layout.tile_count)
    ]
    fixed_bytes = 16 + layout.colors * 2 + len(layout.tile_map) * 2
    original_decompressed_size = fixed_bytes + layout.tile_count * tile_bytes
    maximum_decompressed_size = (
        original_decompressed_size * (100 + CHBG_SIZE_ALLOWANCE_PERCENT) // 100
    )
    capacity_tiles = min(
        0xFFFF,
        (maximum_decompressed_size - fixed_bytes) // tile_bytes,
    )
    if all(pixel == palette[index] for pixel, index in zip(pixels, original_indices)):
        required_tiles = len({original_tiles[index] for index in layout.tile_map})
        return CHBGEncodeResult(
            data=original_raw,
            required_tiles=required_tiles,
            original_tiles=layout.tile_count,
            capacity_tiles=capacity_tiles,
            output_tiles=layout.tile_count,
            original_decompressed_size=original_decompressed_size,
            output_decompressed_size=original_decompressed_size,
            palette_adjusted_pixels=0,
        )

    palette_usage = Counter(original_indices)
    exact_palette_indices: dict[tuple[int, int, int], list[int]] = {}
    for palette_index in range(color_limit):
        exact_palette_indices.setdefault(palette[palette_index], []).append(palette_index)
    # Duplicate RGB values in this format are not interchangeable: one white
    # may belong to scenery while an identical white is part of the menu
    # palette that the game recolors on selection. Restrict edited pixels to
    # indices already used in the same screen region so new lettering keeps
    # the complete palette role (highlight, shadow and outline), not just a
    # visually similar resting color.
    band_height = 32
    # Palette roles on keypad/editor sheets can sit side-by-side inside the
    # same 128-pixel half-screen (for example blue number keys beside green
    # clipboard buttons).  A half-screen domain mixes those banks and can
    # recolor a translated button even when its source pixels use the original
    # palette.  Use sprite-sized horizontal domains instead; empty domains
    # still inherit from their nearest populated neighbour below.
    region_width = 32 if layout.width >= 128 else 128
    region_usage: dict[tuple[int, int], Counter[int]] = {}
    for y_start in range(0, layout.height, band_height):
        for x_start in range(0, layout.width, region_width):
            usage: Counter[int] = Counter()
            for y in range(y_start, min(y_start + band_height, layout.height)):
                row = y * layout.width
                usage.update(original_indices[
                    row + x_start : row + min(x_start + region_width, layout.width)
                ])
            region_usage[(y_start // band_height, x_start // region_width)] = usage
    usable_indices = tuple(range(color_limit))
    non_key_indices = tuple(range(1, color_limit)) or (0,)
    # Editors can shift a sampled BGR555 background by a few RGB values. Treat
    # colors within one quantization step per channel as the key/background,
    # but do not leave index 0 in the general text candidates: doing so could
    # turn a dark outline into transparent/background pixels.
    key_color_distance_limit = 3 * 8 * 8
    # Each populated region defines a palette-role domain. Menu games often
    # animate a selection by changing only that domain's palette entries, so
    # borrowing an equally colored index from another domain looks correct at
    # rest but breaks when highlighted. Empty horizontal regions inherit the
    # nearest populated domain in the same band so longer English labels may
    # safely extend beyond the Japanese label's original width.
    region_roles: dict[tuple[int, int], tuple[tuple[int, ...], Counter[int]]] = {}
    for region, usage in region_usage.items():
        band, column = region
        role_usage = usage
        non_key = tuple(
            index for index in usable_indices
            if index != 0 and usage[index]
        )
        if not non_key:
            siblings: list[tuple[int, Counter[int], tuple[int, ...]]] = []
            for (other_band, other_column), other_usage in region_usage.items():
                if other_band != band:
                    continue
                other_non_key = tuple(
                    index for index in usable_indices
                    if index != 0 and other_usage[index]
                )
                if other_non_key:
                    siblings.append((abs(other_column - column), other_usage, other_non_key))
            if siblings:
                _, role_usage, non_key = min(siblings, key=lambda item: item[0])
        candidates = non_key if non_key else non_key_indices
        region_roles[region] = candidates, role_usage

    nearest_cache: dict[tuple[int, int, tuple[int, int, int]], int] = {}
    raw_indices_array = bytearray()
    palette_adjusted_pixels = 0
    for position, pixel in enumerate(pixels):
        original_index = original_indices[position]
        if original_index < color_limit and pixel == palette[original_index]:
            index = original_index
        elif (allow_global_exact_palette
              and len(exact_palette_indices.get(pixel, ())) == 1):
            # If this RGB color exists at exactly one palette index, there is
            # no competing animated/duplicate role to preserve.  Honor the
            # exact palette color even when translated artwork moves it into
            # a neighbouring sprite region (for example a multicolour logo).
            index = exact_palette_indices[pixel][0]
        elif (
            (pixel[0] - key_color[0]) ** 2
            + (pixel[1] - key_color[1]) ** 2
            + (pixel[2] - key_color[2]) ** 2
            <= key_color_distance_limit
        ):
            index = 0
        else:
            y, x = divmod(position, layout.width)
            region = (y // band_height, x // region_width)
            cache_key = (*region, pixel)
            index = nearest_cache.get(cache_key)
        if index is None:
            y, x = divmod(position, layout.width)
            region = (y // band_height, x // region_width)
            cache_key = (*region, pixel)
            candidates, role_usage = region_roles[region]
            index = min(candidates, key=lambda candidate: (
                (pixel[0] - palette[candidate][0]) ** 2
                + (pixel[1] - palette[candidate][1]) ** 2
                + (pixel[2] - palette[candidate][2]) ** 2,
                -role_usage[candidate],
                -palette_usage[candidate],
                candidate,
            ))
            nearest_cache[cache_key] = index
        raw_indices_array.append(index)
        if pixel != palette[index]:
            palette_adjusted_pixels += 1
    raw_indices = bytes(raw_indices_array)
    tiles_high = layout.height // 8

    desired_tiles: list[bytes] = []
    for ty in range(tiles_high):
        for tx in range(tiles_wide):
            indices = bytearray()
            for py in range(8):
                row = (ty * 8 + py) * layout.width + tx * 8
                indices.extend(raw_indices[row : row + 8])
            if layout.bpp == 4:
                packed = bytearray()
                for pos in range(0, 64, 2):
                    packed.append((indices[pos] & 0xF) | ((indices[pos + 1] & 0xF) << 4))
                tile = bytes(packed)
            else:
                tile = bytes(indices)
            desired_tiles.append(tile)

    tiles, tile_map, required_tiles = _fit_chbg_tiles(
        layout, desired_tiles, original_tiles, capacity_tiles,
    )
    header = bytearray(layout.header)
    struct.pack_into("<H", header, 10, len(tiles))
    result = bytearray(header)
    result.extend(layout.palette_data)
    result.extend(struct.pack(f"<{len(tile_map)}H", *tile_map))
    result.extend(b"".join(tiles))
    output_decompressed_size = len(result)
    if output_decompressed_size > maximum_decompressed_size:
        raise CHBGCapacityError(required_tiles, capacity_tiles, layout.tile_count)
    encoded = ndspy.codeCompression.compress(result, False) if layout.compressed else bytes(result)
    return CHBGEncodeResult(
        data=encoded,
        required_tiles=required_tiles,
        original_tiles=layout.tile_count,
        capacity_tiles=capacity_tiles,
        output_tiles=len(tiles),
        original_decompressed_size=original_decompressed_size,
        output_decompressed_size=output_decompressed_size,
        palette_adjusted_pixels=palette_adjusted_pixels,
    )


def encode_chbg(image: Image.Image, original_raw: bytes,
                compressed: bool | None = None,
                allow_global_exact_palette: bool = False) -> bytes:
    return prepare_chbg_replacement(
        image, original_raw, compressed, allow_global_exact_palette,
    ).data


def export_png(raw: bytes, name: str, destination: Path) -> None:
    decode_chbg(raw, name.lower().endswith(".blz")).save(destination, "PNG")


def normalize_english(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    replacements = {"’": "'", "“": '"', "”": '"', "…": "...", "：": ":", "！": "!", "？": "?"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def _abbreviate_words(text: str) -> str:
    def replace(match: re.Match) -> str:
        word = match.group(0)
        replacement = ABBREVIATIONS.get(word.lower(), word)
        if word.isupper():
            return replacement.upper()
        if word.istitle():
            return replacement.capitalize()
        return replacement

    return re.sub(r"\b[A-Za-z]+\b", replace, text)


def _clean_compact_text(text: str) -> str:
    text = re.sub(r"\s+([?!,:;.])", r"\1", text)
    text = re.sub(r"([(/])\s+", r"\1", text)
    text = re.sub(r"\s+([/)])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip(" ,;:-")


def _semantic_core_candidates(text: str) -> list[str]:
    """Extract short action-oriented wording for cramped menu and help labels."""
    results: list[str] = []
    lower = text.lower()
    negative = bool(re.search(r"\b(?:not|never|cannot|can't|unable|without)\b", lower))
    if not negative and re.search(r"\b(?:run away|escape|flee|retreat)(?: from (?:the )?battle)?\b", lower):
        results.append("RUN")

    use_match = re.search(
        r"(?:this is )?(?:how (?:you )?(?:can |should |to )?use|"
        r"instructions? (?:for|on) (?:how to )?use)\s+(?:this|the|a|an)?\s*(.+?)[.!?]*$",
        text,
        flags=re.I,
    )
    if use_match:
        subject = re.sub(r"\b(?:this|the|a|an)\b", "", use_match.group(1), flags=re.I)
        results.append(_clean_compact_text(f"use {subject}"))

    direct = re.sub(
        r"^(?:this (?:option )?(?:allows|lets) you to|you (?:can|may|should)|"
        r"in order to|it is possible to)\s+",
        "",
        text,
        flags=re.I,
    )
    if direct != text:
        results.append(_clean_compact_text(direct))

    words = text.split()
    core = " ".join(word for word in words if word.strip(".,!?:;").lower() not in AGGRESSIVE_FILLERS)
    core = _clean_compact_text(core)
    if core:
        results.append(core)
    return results


def _word_skeleton(word: str) -> str:
    compact = WORD_COMPACTIONS.get(word.lower())
    if compact:
        return compact
    if len(word) <= 3:
        return word
    consonants = word[0] + re.sub(r"[aeiou]", "", word[1:], flags=re.I)
    return consonants if len(consonants) >= 2 else word[:2]


def _targeted_label(text: str, max_bytes: int) -> str:
    """Make a camel-cased label code that fits an exact ASCII byte budget."""
    token_re = (r"%(?:[-+ #0]*\d*(?:\.\d+)?[diouxXeEfFgGcs%])|~\d+(?:,\d+)*|"
                r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+|[?!]")
    tokens = re.findall(token_re, text)
    if not tokens:
        return ""
    pieces: list[list[str | bool]] = []
    for token in tokens:
        if token.startswith(("%", "~")) or token.isdigit() or token in "?!":
            pieces.append([token, True])
        elif token.lower() in {"cannot", "can't", "unable", "never", "not", "without"}:
            pieces.append(["NO", True])
        else:
            skeleton = _word_skeleton(token)
            pieces.append([skeleton[:1].upper() + skeleton[1:].lower(), False])

    # Punctuation is useful but expendable when a label is extremely tight.
    while sum(len(str(piece[0])) for piece in pieces) > max_bytes:
        punctuation = next((piece for piece in reversed(pieces) if piece[1] and piece[0] in ("?", "!")), None)
        if punctuation:
            pieces.remove(punctuation)
            continue
        word_pieces = [piece for piece in pieces if not piece[1] and len(str(piece[0])) > 1]
        if not word_pieces:
            return ""
        longest = max(word_pieces, key=lambda piece: len(str(piece[0])))
        longest[0] = str(longest[0])[:-1]
    return "".join(str(piece[0]) for piece in pieces)


def shortening_candidates(english: str, max_bytes: int | None = None) -> list[str]:
    """Generate readable-to-aggressive UI abbreviations without truncating words."""
    base = normalize_english(english)
    candidates = [base]

    phrase_compact = base
    for pattern, replacement in PHRASE_SHORTENINGS:
        phrase_compact = re.sub(pattern, replacement, phrase_compact, flags=re.I)
    phrase_compact = _clean_compact_text(phrase_compact)
    candidates.append(phrase_compact)

    abbreviated = _clean_compact_text(_abbreviate_words(phrase_compact))
    candidates.append(abbreviated)

    connective_compact = re.sub(r"\band\b", "&", abbreviated, flags=re.I)
    connective_compact = re.sub(r"\bwith\b", "w/", connective_compact, flags=re.I)
    connective_compact = re.sub(r"\bwithout\b", "w/o", connective_compact, flags=re.I)
    candidates.append(_clean_compact_text(connective_compact))

    words = connective_compact.split()
    keywords = " ".join(word for word in words if word.strip(".,!?:;").lower() not in REMOVABLE_WORDS)
    candidates.append(_clean_compact_text(keywords))

    # Last resort for UI labels: remove vowels from long words while keeping
    # their first/last letters. This is only considered after normal phrases,
    # known abbreviations, and filler removal fail to fit.
    def devowel(match: re.Match) -> str:
        word = match.group(0)
        if len(word) < 7 or word.startswith("%"):
            return word
        middle = re.sub(r"[aeiou]", "", word[1:-1], flags=re.I)
        return word[0] + middle + word[-1]

    candidates.append(_clean_compact_text(re.sub(r"\b[A-Za-z]{7,}\b", devowel, keywords)))
    aggressive_sources = list(candidates)
    semantic_candidates: list[str] = []
    for source in aggressive_sources:
        semantic_candidates.extend(_semantic_core_candidates(source))
    candidates.extend(semantic_candidates)
    if max_bytes is not None:
        label_sources = list(dict.fromkeys(semantic_candidates + aggressive_sources))
        for source in label_sources:
            compact_label = _targeted_label(source, max_bytes)
            if compact_label:
                candidates.append(compact_label)
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


HELP_BOX_COLUMNS = 42
HELP_BOX_LINES = 3


def uses_three_line_help_box(original: str) -> bool:
    """Recognize help prose even when its Japanese source has no hard breaks."""
    source_lines = original.count("\n") + 1
    return source_lines in (2, 3) or (source_lines == 1 and "。" in original)


def wrap_help_box_translation(original: str, english: str) -> str:
    """Wrap automatic text for the common three-row DS help/message box."""
    if not uses_three_line_help_box(original) or not english:
        return english

    normalized = " ".join(english.split())

    def wrapped(candidate: str) -> str | None:
        lines = textwrap.wrap(candidate, width=HELP_BOX_COLUMNS,
                              break_long_words=True, break_on_hyphens=False)
        if (not lines or len(lines) > HELP_BOX_LINES
                or any(len(line) > HELP_BOX_COLUMNS for line in lines)):
            return None
        value = "\n".join(lines)
        return value if format_tokens(original) == format_tokens(value) else None

    direct = wrapped(normalized)
    if direct:
        return direct
    readable_candidates = [normalized]
    phrase_compact = normalized
    for pattern, replacement in PHRASE_SHORTENINGS:
        phrase_compact = re.sub(pattern, replacement, phrase_compact, flags=re.I)
    readable_candidates.append(_clean_compact_text(phrase_compact))
    words = phrase_compact.split()
    readable_candidates.append(_clean_compact_text(" ".join(
        word for word in words
        if word.strip(".,!?:;").lower() not in REMOVABLE_WORDS
    )))
    # Help prose must stay readable. Never use the devowelled/concatenated
    # last-resort label codes used for tiny buttons and one-word fields.
    for candidate in dict.fromkeys(readable_candidates):
        fitted = wrapped(candidate)
        if fitted:
            return fitted
    return english


def fit_translation(original: str, english: str, max_bytes: int) -> str:
    suffix = structural_asset_suffix(original)
    visible_english = _structural_label_prefix(english, suffix) if suffix else english
    visible_budget = max_bytes - len(suffix.encode("ascii"))
    if suffix and visible_budget <= 0:
        return ""
    source_negative = bool(re.search(r"\b(?:no|not|never|cannot|can't|unable|without)\b", english, re.I))
    for candidate in shortening_candidates(visible_english, visible_budget):
        try:
            candidate_negative = bool(re.search(r"\b(?:no|not|never|cannot|can't|unable|without)\b",
                                                candidate, re.I) or candidate.startswith("NO"))
            negative_surrogate = candidate.lower().startswith(("low", "missing", "empty", "disabled"))
            fitted = candidate.rstrip(" :") + suffix
            if (fitted and (not source_negative or candidate_negative or negative_surrogate)
                    and len(fitted.encode("cp932")) <= max_bytes
                    and format_tokens(original) == format_tokens(fitted)
                    and structural_suffix_is_preserved(original, fitted)):
                return fitted
        except UnicodeEncodeError:
            pass
    return ""


def _fullwidth_event_text(text: str) -> str:
    """Convert visible ASCII to CP932 full-width forms, preserving printf tokens."""
    # normalize_english() intentionally trims ordinary UI text, but the
    # acquisition suffix needs its leading separator: it is concatenated
    # directly after an item or money amount.  Remember that separator before
    # normalization and restore it as the full-width CP932 space understood by
    # the event charset.
    leading_space = bool(text and text[0].isspace())
    value = normalize_english(text)
    tokens = list(format_tokens(value))
    markers: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        marker = chr(0xE000 + index)
        value = value.replace(token, marker, 1)
        markers.append((marker, token))
    value = value.upper()
    value = "".join(
        "\u3000" if char == " "
        else chr(ord(char) + 0xFEE0) if 0x21 <= ord(char) <= 0x7E
        else char
        for char in value
    )
    for marker, token in markers:
        value = value.replace(marker, token)
    if leading_space:
        value = "\u3000" + value
    return value


def _pair_serialized_text_is_safe(entry: TextEntry, translation: str) -> bool:
    if entry.key not in PAIR_SERIALIZED_TEXT_KEYS:
        return True
    try:
        raw = translation.encode("cp932")
    except UnicodeEncodeError:
        return False
    if len(raw) > entry.max_bytes or len(raw) % 2:
        return False
    if (entry.key in PAIR_SERIALIZED_PREFIX_KEYS
            and not translation.startswith("\u3000")):
        return False

    # The formatter tokens are consumed before the pairwise charset converter.
    # Every other source character must itself be a two-byte CP932 glyph.
    visible = translation
    for index, token in enumerate(format_tokens(translation)):
        visible = visible.replace(token, chr(0xE000 + index), 1)
    for char in visible:
        if 0xE000 <= ord(char) < 0xE100:
            continue
        try:
            if len(char.encode("cp932")) != 2:
                return False
        except UnicodeEncodeError:
            return False
    return True


def repair_entry_translation(entry: TextEntry, translation: str) -> str:
    """Canonicalize control-sensitive text before it reaches the ROM.

    DS+ easy-event messages pass through a compact Japanese character table.
    ASCII source bytes are reinterpreted as unrelated glyph IDs during
    playback, while an odd byte count also makes the converter skip the NUL
    terminator.  Full-width CP932 Latin text round-trips through both tables.
    """
    if not translation:
        return ""

    if structural_asset_suffix(entry.original):
        return fit_translation(entry.original, translation, entry.max_bytes)

    if entry.key not in PAIR_SERIALIZED_TEXT_KEYS:
        return translation

    body = normalize_english(translation).strip()
    if entry.key in PAIR_SERIALIZED_PREFIX_KEYS:
        body = " " + body.lstrip()
    repaired = _fullwidth_event_text(body)
    if (_pair_serialized_text_is_safe(entry, repaired)
            and translation_is_safe(entry.original, repaired)):
        return repaired

    fallback = _fullwidth_event_text(PAIR_SERIALIZED_TEXT_FALLBACKS[entry.key])
    if (_pair_serialized_text_is_safe(entry, fallback)
            and translation_is_safe(entry.original, fallback)):
        return fallback
    return ""


def entry_translation_is_safe(entry: TextEntry) -> bool:
    if not translation_is_safe(entry.original, entry.translation):
        return False
    if entry.key not in PAIR_SERIALIZED_TEXT_KEYS:
        return True
    return _pair_serialized_text_is_safe(entry, entry.translation)


def quick_translation(entry: TextEntry) -> str:
    exact = EXACT_TRANSLATIONS.get(entry.original)
    if exact:
        return repair_entry_translation(
            entry, fit_translation(entry.original, exact, entry.max_bytes)
        )
    return ""


def _translate_request(strings: list[str], timeout: int = 20) -> list[str]:
    # Newlines delimit requests in this unofficial endpoint. Flatten embedded
    # game line breaks so each source string still maps to exactly one result.
    query = "\n".join(re.sub(r"[\r\n]+", " ", text) for text in strings)
    url = (
        "https://translate.googleapis.com/translate_a/single?client=gtx&sl=ja&tl=en&dt=t&q="
        + urllib.parse.quote(query)
    )
    request = urllib.request.Request(url, headers={"User-Agent": "RPGDS-Translator/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    translated = "".join(part[0] for part in payload[0]).splitlines()
    if len(translated) != len(strings):
        raise ValueError("Translation service returned a mismatched batch")
    return translated


def _online_candidate(text: str) -> bool:
    """Reject control/data fragments that should not be sent as Japanese prose."""
    if has_unsafe_control_chars(text) or "\ufffd" in text:
        return False
    if re.search(r"[\uff61-\uff9f]", text):
        return False
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text))


def auto_translate_entries(entries: Iterable[TextEntry], progress: Callable[[int, int], None] | None = None,
                           online: bool = True, batch_size: int = 12) -> tuple[int, int]:
    pending = [entry for entry in entries if not entry.translation]
    completed = 0
    skipped = 0
    for entry in pending:
        suggestion = quick_translation(entry)
        if suggestion:
            wrapped = wrap_help_box_translation(entry.original, suggestion)
            entry.translation = repair_entry_translation(entry, wrapped)
            entry.auto = True
            completed += 1
    remaining = [entry for entry in pending if not entry.translation]
    if not online:
        return completed, len(remaining)

    eligible = [entry for entry in remaining if _online_candidate(entry.original)]
    skipped += len(remaining) - len(eligible)
    groups: dict[str, list[TextEntry]] = {}
    for entry in eligible:
        groups.setdefault(entry.original, []).append(entry)
    unique = list(groups)
    total = len(unique)
    for start in range(0, total, batch_size):
        batch = unique[start : start + batch_size]
        request_texts = [
            original[:-len(structural_asset_suffix(original))]
            if structural_asset_suffix(original) else original
            for original in batch
        ]
        try:
            results = _translate_request(request_texts)
        except Exception:
            results = []
            for original, request_text in zip(batch, request_texts):
                try:
                    results.extend(_translate_request([request_text]))
                except Exception:
                    results.append("")
        for original, result in zip(batch, results):
            for entry in groups[original]:
                if uses_three_line_help_box(entry.original):
                    suggestion = wrap_help_box_translation(entry.original, result)
                else:
                    suggestion = fit_translation(entry.original, result, entry.max_bytes)
                if suggestion:
                    wrapped = wrap_help_box_translation(entry.original, suggestion)
                    suggestion = repair_entry_translation(entry, wrapped)
                if suggestion:
                    entry.translation = suggestion
                    entry.auto = True
                    completed += 1
                else:
                    skipped += 1
        if progress:
            progress(min(start + len(batch), total), total)
    return completed, skipped


def _pointer_index(data: bytes | bytearray) -> dict[int, list[int]]:
    """Index aligned ARM literal/table values in one load unit."""
    index: dict[int, list[int]] = {}
    for offset in range(0, len(data) - 3, 4):
        value = struct.unpack_from("<I", data, offset)[0]
        index.setdefault(value, []).append(offset)
    return index


def _pack_existing_slots(entries: list[TextEntry], slot_ends: dict[str, int]) -> dict[str, int] | None:
    """Best-fit strings into the selected entries' existing contiguous slots."""
    ranges = sorted((entry.offset, slot_ends[entry.key]) for entry in entries)
    # First try a stable one-string-per-slot reassignment. This avoids greedy
    # fragmentation when a sentence only needs to trade slots with a slightly
    # shorter translation.
    payloads = sorted(entries, key=lambda entry: (-len(entry.translation.encode("cp932")),
                                                   entry.offset))
    slots = sorted(ranges, key=lambda pair: (-(pair[1] - pair[0]), pair[0]))
    if all(len(entry.translation.encode("cp932")) + 1 <= end - start
           for entry, (start, end) in zip(payloads, slots)):
        return {entry.key: start for entry, (start, _end) in zip(payloads, slots)}

    bins: list[list[int]] = []
    for start, end in ranges:
        if bins and start <= bins[-1][1]:
            bins[-1][1] = max(bins[-1][1], end)
        else:
            bins.append([start, end])

    cursors = [start for start, _ in bins]
    allocations: dict[str, int] = {}
    for entry in payloads:
        needed = len(entry.translation.encode("cp932")) + 1
        choices = [(bins[index][1] - cursors[index] - needed, index)
                   for index in range(len(bins))
                   if bins[index][1] - cursors[index] >= needed]
        if not choices:
            return None
        _, index = min(choices)
        allocations[entry.key] = cursors[index]
        cursors[index] += needed
    return allocations


def _apply_region_entries(data: bytearray, ram_address: int,
                          entries: list[TextEntry], location: str,
                          allow_relocation: bool = True) -> int:
    """Apply one load unit without changing its size or runtime memory layout."""
    for entry in entries:
        if not entry.translation:
            continue
        repaired = repair_entry_translation(entry, entry.translation)
        if not repaired:
            if structural_asset_suffix(entry.original):
                raise ValueError(
                    f"Structural asset label at {location}, 0x{entry.offset:X} cannot fit "
                    f"its required {structural_asset_suffix(entry.original)} suffix"
                )
            if entry.key in PAIR_SERIALIZED_TEXT_KEYS:
                raise ValueError(
                    f"Pair-serialized text at {location}, 0x{entry.offset:X} must fit "
                    "its original slot and use the full-width CP932 event charset"
                )
            raise ValueError(f"Invalid translation at {location}, 0x{entry.offset:X}")
        entry.translation = repaired
    translated = [entry for entry in entries if entry.translation]
    if not translated:
        return 0

    pointer_refs: dict[str, list[int]] = {}
    slot_ends: dict[str, int] = {}
    movable: list[TextEntry] = []
    pointers = _pointer_index(data)
    for entry in translated:
        if not entry_translation_is_safe(entry):
            raise ValueError(f"Invalid translation at {location}, 0x{entry.offset:X}")
        original_raw = entry.original.encode("cp932")
        if bytes(data[entry.offset : entry.offset + len(original_raw)]) != original_raw:
            raise ValueError(f"Original text mismatch at {location}, 0x{entry.offset:X}")
        refs = pointers.get(entry.address, [])
        pointer_refs[entry.key] = refs
        terminator = entry.offset + entry.max_bytes
        # A missing direct pointer indicates a computed/relative string. A
        # non-NUL boundary indicates packed data. Neither can be relocated.
        in_non_relocatable_range = any(
            entry.overlay == overlay_id and start <= entry.offset < end
            for overlay_id, start, end in NON_RELOCATABLE_RANGES
        )
        # Catalog labels ending in :NNN are parsed as resource metadata.  Keep
        # them at their original addresses and never recruit their slots as
        # donors for unrelated extended prose.
        if (entry.key not in NON_RELOCATABLE_KEYS and not in_non_relocatable_range
                and not structural_asset_suffix(entry.original) and refs
                and terminator < len(data) and data[terminator] == 0):
            movable.append(entry)
            end = terminator + 1
            aligned_end = min((end + 3) & ~3, len(data))
            # Include only verified NUL alignment padding. This can provide the
            # final 1-3 bytes a wrapped sentence needs without touching data.
            if all(value == 0 for value in data[end:aligned_end]):
                end = aligned_end
            slot_ends[entry.key] = end

    overlong = [entry for entry in translated if entry.used_bytes > entry.max_bytes]
    if overlong and not allow_relocation:
        entry = overlong[0]
        raise ValueError(
            f"Text at {location}, 0x{entry.offset:X} needs {entry.used_bytes} bytes, "
            f"but DS+ stability mode allows only its original {entry.max_bytes}-byte slot"
        )
    fixed_overlong = [entry for entry in overlong if entry not in movable]
    if fixed_overlong:
        entry = fixed_overlong[0]
        raise ValueError(
            f"Text at {location}, 0x{entry.offset:X} is reached by a computed offset "
            "and must remain within its original byte limit"
        )

    selected: list[TextEntry] = list(overlong)
    allocations = _pack_existing_slots(selected, slot_ends) if selected else {}
    if selected and allocations is None:
        donors = sorted(
            (entry for entry in movable if entry not in selected),
            key=lambda entry: (-(entry.max_bytes - entry.used_bytes), -entry.max_bytes,
                               entry.offset),
        )
        for donor in donors:
            selected.append(donor)
            allocations = _pack_existing_slots(selected, slot_ends)
            if allocations is not None:
                break
    if selected and allocations is None:
        needed = sum(entry.used_bytes + 1 for entry in selected)
        available = sum(slot_ends[entry.key] - entry.offset for entry in selected)
        raise ValueError(
            f"The translated text pool in {location} cannot fit safely "
            f"({needed} bytes needed, {available} bytes available)"
        )

    selected_keys = {entry.key for entry in selected}
    # Ordinary replacements retain the old minimal-change behavior.
    for entry in translated:
        if entry.key in selected_keys:
            continue
        raw = entry.translation.encode("cp932")
        data[entry.offset : entry.offset + entry.max_bytes] = (
            raw + b"\0" * (entry.max_bytes - len(raw))
        )

    if selected:
        # Clear only verified original string slots, including their NUL byte.
        for entry in selected:
            end = slot_ends[entry.key]
            data[entry.offset:end] = b"\0" * (end - entry.offset)
        for entry in selected:
            new_offset = allocations[entry.key]
            raw = entry.translation.encode("cp932") + b"\0"
            data[new_offset : new_offset + len(raw)] = raw
        # Patch only references in the string's owning load unit. Overlay RAM
        # addresses overlap by design, so identical values in other overlays
        # must not be changed.
        for entry in selected:
            new_address = ram_address + allocations[entry.key]
            for ref_offset in pointer_refs[entry.key]:
                struct.pack_into("<I", data, ref_offset, new_address)
    return len(translated)


def apply_entries(rom: ndspy.rom.NintendoDSRom, entries: Iterable[TextEntry]) -> int:
    profile = profile_for_rom(rom)
    overlays = rom.loadArm9Overlays()
    arm9_data = bytearray(ndspy.codeCompression.decompress(bytes(rom.arm9)))
    grouped: dict[int, list[TextEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.overlay, []).append(entry)

    changed: set[int] = set()
    count = 0
    arm9_count = _apply_region_entries(
        arm9_data, rom.arm9RamAddress, grouped.get(-1, []), "ARM9",
        profile.allow_text_relocation,
    )
    count += arm9_count
    for overlay_id, region_entries in grouped.items():
        if overlay_id == -1:
            continue
        overlay = overlays[overlay_id]
        applied = _apply_region_entries(
            overlay.data, overlay.ramAddress, region_entries, f"overlay {overlay_id}",
            profile.allow_text_relocation and overlay_id not in profile.fixed_slot_overlays,
        )
        if applied:
            changed.add(overlay_id)
            count += applied
    if arm9_count:
        rom.arm9 = _compress_arm9(bytes(arm9_data), rom.arm9RamAddress)
    for overlay_id in changed:
        overlay = overlays[overlay_id]
        rom.files[overlay.fileID] = overlay.save(compress=True)
    if changed:
        rom.arm9OverlayTable = ndspy.code.saveOverlayTable(overlays)
    return count


def compile_rom(source_rom: Path, output_rom: Path, entries: Iterable[TextEntry],
                image_pngs: dict[str, bytes]) -> tuple[int, int]:
    rom = ndspy.rom.NintendoDSRom.fromFile(source_rom)
    text_count = apply_entries(rom, entries)
    image_count = 0
    for name, png_data in image_pngs.items():
        original = bytes(rom.getFileByName(name))
        with Image.open(io.BytesIO(png_data)) as source_image:
            image = sanitize_import_image(source_image)
        try:
            encoded = encode_chbg(
                image, original, name.lower().endswith(".blz"),
                name.lower() == "wifi/castle-logo.bin",
            )
        except ValueError as exc:
            raise ValueError(f"Image replacement {name}: {exc}") from exc
        rom.setFileByName(name, encoded)
        image_count += 1
    rom.saveToFile(output_rom, updateDeviceCapacity=True)
    return text_count, image_count


def save_project(path: Path, source_rom: Path, entries: Iterable[TextEntry], image_pngs: dict[str, bytes]) -> None:
    metadata = {"version": PROJECT_VERSION, "source_rom": str(source_rom),
                "source_sha256": sha256_file(source_rom), "images": {}}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=("overlay", "offset", "address", "max_bytes",
                                                    "original", "translation", "auto"))
        writer.writeheader()
        for entry in entries:
            if entry.translation:
                repaired = repair_entry_translation(entry, entry.translation)
                if not repaired:
                    detail = (
                        f"its required {structural_asset_suffix(entry.original)} suffix"
                        if structural_asset_suffix(entry.original)
                        else "its runtime serialization constraints"
                    )
                    raise ValueError(f"Translation {entry.key} cannot fit {detail}")
                entry.translation = repaired
            row = asdict(entry)
            row["offset"] = f"0x{entry.offset:X}"
            row["address"] = f"0x{entry.address:X}"
            writer.writerow(row)
        archive.writestr("translations.csv", "\ufeff" + buffer.getvalue())
        for index, (name, png) in enumerate(sorted(image_pngs.items())):
            member = f"images/{index:04d}.png"
            metadata["images"][name] = member
            archive.writestr(member, sanitize_png_bytes(png))
        archive.writestr("project.json", json.dumps(metadata, indent=2))


def load_project(path: Path) -> tuple[Path, dict[str, dict], dict[str, bytes]]:
    with zipfile.ZipFile(path, "r") as archive:
        metadata = json.loads(archive.read("project.json"))
        rows: dict[str, dict] = {}
        text = archive.read("translations.csv").decode("utf-8-sig")
        for row in csv.DictReader(io.StringIO(text)):
            translation = row.get("translation", "")
            if translation:
                temporary = TextEntry(
                    overlay=int(row["overlay"]),
                    offset=int(row["offset"], 0),
                    address=int(row["address"], 0),
                    max_bytes=int(row["max_bytes"]),
                    original=row["original"],
                    translation=translation,
                )
                repaired = repair_entry_translation(temporary, translation)
                if not repaired:
                    detail = (
                        f"its required {structural_asset_suffix(row['original'])} suffix"
                        if structural_asset_suffix(row["original"])
                        else "its runtime serialization constraints"
                    )
                    raise ValueError(
                        f"Project translation at overlay {row['overlay']}, {row['offset']} "
                        f"cannot fit {detail}"
                    )
                row["translation"] = repaired
            key = f"{int(row['overlay'])}:{int(row['offset'], 0):X}"
            rows[key] = row
        images = {name: archive.read(member) for name, member in metadata.get("images", {}).items()}
    return Path(metadata["source_rom"]), rows, images
