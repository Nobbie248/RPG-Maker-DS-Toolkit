import unittest

from rpgds_gui import image_asset_category


class GraphicsCategoryTests(unittest.TestCase):
    def test_primary_asset_groups(self):
        expected = {
            "monster/001.bin": "Monsters",
            "face/face0_00.bin": "Characters",
            "chibitsuku/0-01.bin": "Characters",
            "play/chara/chara00.bin": "Characters",
            "item/icon00.bin": "Items",
            "town/town-parts01.blz::texture": "Parts",
            "dungeon/dungeon01-parts.blz::texture": "Parts",
            "room/room-parts01.blz::texture": "Parts",
            "field/field.ebba::texture": "Parts",
            "event/event-obj01.ebba::texture": "Parts",
            "effect/001.blz::texture": "Effects",
            "fukidashi/01::texture": "Effects",
            "title/bg01.blz": "Backgrounds",
            "map/dungeon-bg.bin": "Backgrounds",
            "topmenu/topmenu.bin": "Title",
            "title/title-logo.bin": "Title",
            "edit/edit-part.blz": "Interface",
            "wifi/castle-logo.bin": "Interface",
            "play/spotlight.bin": "Misc",
        }
        for name, category in expected.items():
            with self.subTest(name=name):
                self.assertEqual(image_asset_category(name), category)


if __name__ == "__main__":
    unittest.main()
