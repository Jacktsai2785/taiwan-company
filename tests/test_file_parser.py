import io
import unittest

import fitz

from services import file_parser


def _build_pdf_with_image_and_text_pages() -> bytes:
    """建一份兩頁的測試 PDF：第一頁整頁是圖片（沒有文字層），第二頁是純文字。
    模擬真實案例——BP 簡報常見的封面/團隊介紹用設計排版（圖片），跟財務數字表格
    (純文字) 混在同一份 PDF 裡。"""
    from PIL import Image

    img_buf = io.BytesIO()
    Image.new("RGB", (100, 60), color=(200, 50, 50)).save(img_buf, format="PNG")
    img_bytes = img_buf.getvalue()

    doc = fitz.open()
    page1 = doc.new_page(width=200, height=120)
    page1.insert_image(fitz.Rect(0, 0, 200, 120), stream=img_bytes)
    page2 = doc.new_page(width=200, height=120)
    page2.insert_text((10, 30), "測試財務數字：營收100萬元")
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class ExtractPdfImageOnlyPagesTests(unittest.TestCase):
    def test_returns_only_the_image_only_page(self):
        pdf_bytes = _build_pdf_with_image_and_text_pages()

        pages = file_parser.extract_pdf_image_only_pages(pdf_bytes)

        self.assertEqual(len(pages), 1)
        ext, img_bytes = pages[0]
        self.assertTrue(img_bytes)
        self.assertIn(ext, ("png", "jpeg", "jpg"))

    def test_pure_text_pdf_returns_nothing(self):
        doc = fitz.open()
        page = doc.new_page(width=200, height=120)
        page.insert_text((10, 30), "全部都是文字，沒有圖片頁")
        pdf_bytes = doc.tobytes()
        doc.close()

        pages = file_parser.extract_pdf_image_only_pages(pdf_bytes)

        self.assertEqual(pages, [])

    def test_malformed_pdf_degrades_to_empty_list_not_exception(self):
        pages = file_parser.extract_pdf_image_only_pages(b"not a real pdf")
        self.assertEqual(pages, [])


if __name__ == "__main__":
    unittest.main()
