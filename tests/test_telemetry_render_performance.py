from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parents[1]
APP = REPO / "projects" / "vehicle_data" / "static" / "app.js"


def function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    next_function = source.find("\nfunction ", start + 1)
    return source[start:] if next_function < 0 else source[start:next_function]


class TelemetryRenderPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.source = APP.read_text()

    def test_text_and_card_state_writes_are_idempotent(self):
        text_source = function_source(self.source, "text")
        state_source = function_source(self.source, "setCardState")

        self.assertIn("element.textContent !== next", text_source)
        self.assertIn("card.dataset.state !== state", state_source)

    def test_live_render_does_not_rebuild_minute_level_products(self):
        render = function_source(self.source, "render")

        self.assertNotIn("renderEarlyWarnings", render)
        self.assertNotIn("renderHistory", render)
        self.assertNotIn("renderDtcs", render)
        supplemental = function_source(self.source, "fetchSupplemental")
        self.assertIn("renderEarlyWarnings", supplemental)
        self.assertIn("renderHistory", supplemental)
        self.assertIn("renderDtcs", supplemental)

    def test_structural_dom_is_reused_until_its_input_shape_changes(self):
        builder = function_source(self.source, "buildMetricCard")
        additional = function_source(self.source, "renderAdditionalMetrics")
        catalog = function_source(self.source, "renderCatalog")
        interface = function_source(self.source, "renderInterface")

        self.assertNotIn("state.", builder)
        self.assertIn("additionalMetricStructureKey", additional)
        self.assertIn("additionalMetricNodes.get", additional)
        self.assertIn("catalogSignature === lastCatalogSignature", catalog)
        self.assertIn("roleSignature === lastRoleGridSignature", interface)

    def test_freshness_watchdog_skips_duplicate_healthy_stream_render(self):
        freshness = function_source(self.source, "freshnessTick")

        self.assertIn(
            "nowMonotonicMs - lastAcceptedMonotonicMs >= FRESHNESS_TICK_MS",
            freshness,
        )
        self.assertIn("advanceDisplayedAges", freshness)
        self.assertIn("STREAM_STALL_RESYNC_MS", freshness)


if __name__ == "__main__":
    unittest.main()
