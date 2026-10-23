"""word_convert.py 单元测试：文件名识别（不依赖本机是否安装 Word / COM）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from word_convert import is_word_filename, word_available, WordConvertError


class TestWordFilename(unittest.TestCase):
    def test_pdf_is_not_word(self):
        self.assertFalse(is_word_filename("paper.pdf"))
        self.assertFalse(is_word_filename("PAPER.PDF"))

    def test_docx_doc_are_word(self):
        self.assertTrue(is_word_filename("paper.docx"))
        self.assertTrue(is_word_filename("paper.DOCX"))
        self.assertTrue(is_word_filename("old-format.doc"))

    def test_lookalike_names_rejected(self):
        # 伪装扩展名 / 无扩展名不应被当成 Word
        self.assertFalse(is_word_filename("paper.docx.exe"))
        self.assertFalse(is_word_filename("paperdocx"))
        self.assertFalse(is_word_filename(""))
        self.assertFalse(is_word_filename(None))


class TestAvailabilityProbe(unittest.TestCase):
    def test_probe_returns_bool(self):
        # 本机为 Windows + 已装 pywin32 时应为 True；此处只断言类型，避免环境耦合
        self.assertIsInstance(word_available(), bool)


class TestErrorType(unittest.TestCase):
    def test_error_is_exception(self):
        self.assertTrue(issubclass(WordConvertError, Exception))


if __name__ == "__main__":
    unittest.main()
