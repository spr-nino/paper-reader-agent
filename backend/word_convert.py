"""Word（.doc / .docx）转 PDF：调用本机 Microsoft Word（COM）转换，之后走原有 PDF 解析管线。

设计要点：
- 仅 Windows 且已安装 pywin32 + Microsoft Word 时可用；其余环境抛出 WordConvertError，
  引导用户改为「另存为 PDF」后上传（与前端提示语呼应）。
- DispatchEx 启动独立 Word 进程，不干扰用户已打开的文档窗口；
- 只读打开、不加入最近文件列表、强制禁用宏（AutomationSecurity=ForceDisable）、
  关闭所有弹窗告警，避免转换过程被对话框阻塞。
- COM 要求调用线程先 CoInitialize，finally 中配对 CoUninitialize 并确保进程退出。
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

WORD_EXTENSIONS = (".docx", ".doc")

# Word / Office 常量
WD_FORMAT_PDF = 17                      # wdFormatPDF
WD_ALERTS_NONE = 0                      # wdAlertsNone
MSO_FORCE_DISABLE_MACROS = 3            # msoAutomationSecurityForceDisable


class WordConvertError(Exception):
    """Word 转 PDF 失败（环境不支持 / COM 异常 / Word 无法打开该文件）。"""


def is_word_filename(filename: str) -> bool:
    """判断文件名是否为受支持的 Word 格式（.docx / .doc，大小写不敏感）。"""
    return (filename or "").lower().endswith(WORD_EXTENSIONS)


def word_available() -> bool:
    """探测当前环境是否具备 Word COM 转换能力（Windows + pywin32）。"""
    if os.name != "nt":
        return False
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    return True


def word_to_pdf(src_path, dst_path) -> None:
    """将 .doc / .docx 转为 PDF。任何失败均抛出 WordConvertError（中文提示）。"""
    if not word_available():
        raise WordConvertError(
            "当前服务端环境不支持直接解析 Word 文档（需要 Windows + Microsoft Word）。"
            "请在 Word 中将文件「另存为 PDF」后再上传。"
        )
    _word_com_to_pdf(str(Path(src_path).resolve()), str(Path(dst_path).resolve()))


def _word_com_to_pdf(src: str, dst: str) -> None:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = WD_ALERTS_NONE
        word.ScreenUpdating = False
        # 强制禁用文档宏，防止转换时执行文档内 VBA
        word.AutomationSecurity = MSO_FORCE_DISABLE_MACROS

        doc = word.Documents.Open(
            FileName=src,
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
        )
        doc.SaveAs2(FileName=dst, FileFormat=WD_FORMAT_PDF)
        logger.info("Word→PDF 转换完成：%s -> %s", src, dst)
    except Exception as e:
        raise WordConvertError(
            f"Word 转 PDF 失败（{type(e).__name__}: {e}）。"
            "可先在 Word 中打开该文件确认能正常显示，另存为 PDF 后再上传。"
        ) from e
    finally:
        try:
            if doc is not None:
                doc.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit(SaveChanges=False)
        except Exception:
            pass
        pythoncom.CoUninitialize()
