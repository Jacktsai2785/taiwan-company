from services.patent_scraper import _is_exact_applicant


def test_exact_applicant_rejects_similarly_named_company():
    legal_name = "東佑達自動化科技股份有限公司"

    assert _is_exact_applicant(legal_name, legal_name)
    assert _is_exact_applicant("東佑達自動化科技 股份有限公司", legal_name)
    assert not _is_exact_applicant("東佑達奈米系統股份有限公司", legal_name)
    assert not _is_exact_applicant("東佑達自動化科技", legal_name)
    assert not _is_exact_applicant("", legal_name)
