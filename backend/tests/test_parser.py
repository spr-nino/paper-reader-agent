"""parser.py 单元测试：P0-4 扫描版检测、OCR 兜底与章节为空检测"""
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz
from PIL import Image, ImageDraw, ImageFont

import parser


def _tesseract_available() -> bool:
    """检测 Tesseract OCR 引擎是否真正可用。"""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


class TestParser(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.doc_id = "test_doc_p0_4"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        data_dir = parser.DATA_DIR / self.doc_id
        if data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)

    def _make_image_pdf(self, path: str, text: str = None):
        """生成仅含图片、无文字层的 PDF，用于模拟扫描件。"""
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        img = Image.new("RGB", (612, 792), "white")
        if text:
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("arial.ttf", 24)
            except Exception:
                font = ImageFont.load_default()
            draw.text((60, 80), text, fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(path)
        doc.close()

    def _make_text_pdf(self, path: str, add_heading: bool = True):
        """生成带文字层的普通 PDF。"""
        doc = fitz.open()
        for i in range(2):
            page = doc.new_page(width=612, height=792)
            body = (
                "This is a normal text layer page with enough characters "
                "to pass the scanned PDF detection threshold. " * 20
            )
            page.insert_text((72, 100 + i * 300), body)
            if add_heading and i == 0:
                page.insert_text((72, 60), "1 Introduction", fontsize=18)
        doc.save(path)
        doc.close()

    def test_blank_scanned_pdf_raises_scanned_error(self):
        """纯空白扫描 PDF 应被识别并抛出 ScannedPDFError。"""
        path = os.path.join(self.tmpdir, "blank_scan.pdf")
        self._make_image_pdf(path)
        with self.assertRaises(parser.ScannedPDFError):
            parser.parse_pdf(path, self.doc_id)

    def test_scanned_pdf_without_tesseract_raises(self):
        """无 Tesseract 引擎时，扫描 PDF 应直接返回扫描版错误。"""
        if _tesseract_available():
            self.skipTest("Tesseract 已安装，跳过无引擎场景")
        path = os.path.join(self.tmpdir, "scan_no_engine.pdf")
        self._make_image_pdf(path, text="Some text on a scanned page.")
        with self.assertRaises(parser.ScannedPDFError):
            parser.parse_pdf(path, self.doc_id)

    @unittest.skipUnless(_tesseract_available(), "需要本机安装 Tesseract")
    def test_scanned_pdf_with_ocr_text_uses_fallback(self):
        """带文字的扫描图在 OCR 可用时应成功解析，并退化为单章节。"""
        path = os.path.join(self.tmpdir, "scan_with_text.pdf")
        text = ("Introduction. This scanned page contains real text. " * 40) + "\n"
        self._make_image_pdf(path, text=text)
        result = parser.parse_pdf(path, self.doc_id)
        self.assertTrue(result["sections"])
        self.assertEqual(result["sections"][0]["title"], "全文")
        self.assertTrue(result["chunks"])
        for chunk in result["chunks"]:
            self.assertIsInstance(chunk["page_from"], int)
            self.assertLessEqual(chunk["page_from"], chunk["page_to"])

    def test_normal_text_pdf_parses_with_real_pages(self):
        """普通文字版 PDF 应正常解析，且 chunk 页码在合理范围内。"""
        path = os.path.join(self.tmpdir, "normal.pdf")
        self._make_text_pdf(path)
        result = parser.parse_pdf(path, self.doc_id)
        self.assertTrue(result["sections"])
        self.assertTrue(result["chunks"])
        for chunk in result["chunks"]:
            self.assertGreaterEqual(chunk["page_from"], 1)
            self.assertLessEqual(chunk["page_to"], result["n_pages"])

    def test_no_sections_raises_parse_error(self):
        """文字层正常但无法提取任何章节时，应抛出 ParseError。"""
        path = os.path.join(self.tmpdir, "no_sections.pdf")
        self._make_text_pdf(path, add_heading=False)
        with self.assertRaises(parser.ParseError):
            parser.parse_pdf(path, self.doc_id)


if __name__ == "__main__":
    unittest.main()
