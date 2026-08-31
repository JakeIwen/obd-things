from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parents[1]
STYLE = REPO / "projects" / "vehicle_data" / "static" / "style.css"


class TelemetryTabletLayoutTests(unittest.TestCase):
    def test_tablet_breakpoints_and_balanced_primary_grids_are_explicit(self):
        css = STYLE.read_text()

        self.assertIn('@media (max-width: 64rem)', css)
        self.assertIn(
            '@media (min-width: 64.001rem) and (max-width: 82rem)',
            css,
        )
        self.assertIn(
            'grid-template-columns: minmax(0, 1.5fr) repeat(4, minmax(0, 1fr));',
            css,
        )
        self.assertIn(
            '.engine-health-grid {\n  display: grid;\n'
            '  grid-template-columns: repeat(3, minmax(0, 1fr));',
            css,
        )
        self.assertIn('.drive-primary { grid-column: span 2; }', css)

    def test_long_status_content_has_defensive_overflow_rules(self):
        css = STYLE.read_text()

        self.assertIn(
            'body {\n  margin: 0;\n  min-height: 100vh;\n'
            '  overflow-x: hidden;\n  overflow-x: clip;',
            css,
        )
        self.assertIn('.section-heading {\n  display: flex;\n  flex-wrap: wrap;', css)
        self.assertIn('overflow-wrap: anywhere;\n  text-align: center;', css)
        self.assertIn('.metric-quality {\n    position: static;', css)


if __name__ == "__main__":
    unittest.main()
