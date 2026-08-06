import unittest

from routers.findbiz import _is_cloudflare_challenge, _is_no_par_value, _parse_detail_html
from routers.enrichment import _nonempty_fields


class FindbizChallengeTests(unittest.TestCase):
    def test_detects_cloudflare_page(self):
        self.assertTrue(
            _is_cloudflare_challenge(
                "<html><head><title>Just a moment...</title></head></html>"
            )
        )
        self.assertTrue(_is_cloudflare_challenge("<script>window._cf_chl_opt={}</script>"))

    def test_normal_page_cloudflare_script_is_not_a_challenge(self):
        # Normal pages can preload Cloudflare's challenge-platform script.
        self.assertFalse(
            _is_cloudflare_challenge(
                '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
            )
        )

    def test_company_detail_is_not_a_challenge(self):
        html = """
        <table>
          <tr><td>統一編號</td><td>60711057</td></tr>
          <tr><td>每股金額(元)</td><td>10</td></tr>
          <tr><td>已發行股份總數(股)</td><td>210,000</td></tr>
        </table>
        """
        self.assertFalse(_is_cloudflare_challenge(html))
        parsed = _parse_detail_html(html)
        self.assertEqual(parsed["統一編號"], "60711057")
        self.assertEqual(parsed["每股金額(元)"], "10")
        self.assertEqual(parsed["已發行股份總數(股)"], "210,000")

    def test_no_par_value_is_a_real_source_value(self):
        html = """
        <table>
          <tr><td>每股金額(元)</td><td>無票面金額</td></tr>
          <tr><td>已發行股份總數(股)</td><td>209,400</td></tr>
        </table>
        """
        parsed = _parse_detail_html(html)
        self.assertTrue(_is_no_par_value(parsed["每股金額(元)"]))

    def test_false_boolean_is_not_filtered_as_numeric_zero(self):
        self.assertEqual(
            _nonempty_fields({"par_value": 0, "no_par_value": False, "address": ""}),
            {"no_par_value": False},
        )

    def test_missing_par_value_is_not_no_par_value(self):
        self.assertFalse(_is_no_par_value(""))


if __name__ == "__main__":
    unittest.main()
