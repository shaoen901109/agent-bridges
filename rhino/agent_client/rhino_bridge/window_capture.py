# encoding: utf-8
"""Windows 全視窗截圖（僅觀看，不做任何滑鼠/鍵盤輸入模擬）。

流程：
  1. 用 ctypes + EnumWindows 找出屬於 Rhino.exe 的可見最上層視窗（Rhino 主視窗、Grasshopper 編輯器、對話框都算）。
  2. 優先用 PrintWindow(PW_RENDERFULLCONTENT)——即使視窗被其他視窗遮住也能擷取。
  3. PrintWindow 失敗或畫面全黑時，退回 PIL ImageGrab（需要視窗實際可見），
     並在結果中註明實際使用的方法。
  4. 處理 DPI 縮放（宣告 per-monitor DPI aware）與視窗最小化的情況。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import sys
from typing import Any, Dict, List, Optional

DEFAULT_PROCESS_NAME = "Rhino.exe"

_IS_WINDOWS = sys.platform == "win32"


class WindowCaptureError(RuntimeError):
    """找不到視窗或擷取失敗。"""


# --- Win32 常數 -----------------------------------------------------------

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SW_RESTORE = 9


def _ensure_windows() -> None:
    if not _IS_WINDOWS:
        raise WindowCaptureError("全視窗截圖只支援 Windows。")


_dpi_awareness_set = False


def _set_dpi_awareness() -> None:
    """讓本行程使用實體像素座標，否則在 >100% 縮放時截圖會被系統拉伸模糊。"""
    global _dpi_awareness_set
    if _dpi_awareness_set or not _IS_WINDOWS:
        return
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2（Windows 10 1703+）
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    _dpi_awareness_set = True


# --- 視窗列舉 -------------------------------------------------------------


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _process_image_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _window_rect(hwnd: int) -> tuple:
    """回傳 (left, top, right, bottom)，優先使用 DWM 的實際外框（避免陰影邊界）。"""
    rect = wintypes.RECT()
    try:
        hresult = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hresult == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom)


def find_windows(
    process_name: str = DEFAULT_PROCESS_NAME,
    title_contains: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """列出符合條件的可見最上層視窗，面積大的排前面。"""
    _ensure_windows()
    user32 = ctypes.windll.user32
    results: List[Dict[str, Any]] = []
    target = (process_name or "").lower()

    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe_path = _process_image_name(pid.value)
        exe_name = os.path.basename(exe_path).lower()
        title = _window_title(hwnd)

        if target and exe_name != target:
            return True
        if title_contains and title_contains.lower() not in title.lower():
            return True
        if not title and not target:
            return True

        left, top, right, bottom = _window_rect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return True

        results.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "pid": int(pid.value),
                "exe": exe_path,
                "rect": [left, top, right, bottom],
                "width": width,
                "height": height,
                "minimized": bool(user32.IsIconic(hwnd)),
            }
        )
        return True

    user32.EnumWindows(enum_proc_type(_callback), 0)
    results.sort(key=lambda w: w["width"] * w["height"], reverse=True)
    return results


# --- 擷取 -----------------------------------------------------------------


def _capture_print_window(hwnd: int):
    """用 PrintWindow 擷取視窗；回傳 PIL.Image 或 None。"""
    from PIL import Image  # 延後 import，讓沒裝 Pillow 時錯誤訊息更清楚

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    left, top, right, bottom = _window_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = None
    try:
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # 負值 = top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        bits = ctypes.c_void_p()
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        bitmap = gdi32.CreateDIBSection(
            mem_dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits:
            return None

        gdi32.SelectObject(mem_dc, ctypes.c_void_p(bitmap))
        ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        if not ok:
            # 有些視窗只支援舊旗標
            ok = user32.PrintWindow(hwnd, mem_dc, 0)
        if not ok:
            return None

        buffer = ctypes.string_at(bits, width * height * 4)
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
        return image
    finally:
        if bitmap:
            gdi32.DeleteObject(ctypes.c_void_p(bitmap))
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)


def _looks_blank(image) -> bool:
    """判斷截圖是否幾乎全黑/全白（PrintWindow 對某些 GPU 視窗會失敗）。"""
    try:
        small = image.convert("L").resize((32, 32))
        pixels = list(small.getdata())
        return (max(pixels) - min(pixels)) < 4
    except Exception:
        return False


def _capture_image_grab(rect):
    from PIL import ImageGrab

    left, top, right, bottom = rect
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)


def capture_window(
    process_name: str = DEFAULT_PROCESS_NAME,
    title_contains: Optional[str] = None,
    hwnd: Optional[int] = None,
    restore_if_minimized: bool = False,
) -> Dict[str, Any]:
    """擷取 Rhino 主視窗（或用 title_contains 指定 Grasshopper 編輯器等其他視窗）。

    回傳 dict：image(PIL.Image)、method('PrintWindow'/'ImageGrab')、window(視窗資訊)、
    notes(list[str] 說明退回原因或注意事項)。
    """
    _ensure_windows()
    _set_dpi_awareness()

    notes: List[str] = []

    if hwnd is None:
        windows = find_windows(process_name=process_name, title_contains=title_contains)
        if not windows:
            raise WindowCaptureError(
                f"找不到 {process_name} 的可見視窗。\n"
                "請確認 Rhino 已經開啟，而且不是完全最小化到系統匣。\n"
                "（這個工具只負責觀看，不會自行啟動或關閉 Rhino；要啟動請用 rhino_launch。）"
            )
        window = windows[0]
        if len(windows) > 1:
            notes.append(
                f"找到 {len(windows)} 個視窗，已選擇面積最大的「{window['title']}」。"
            )
    else:
        matches = [w for w in find_windows(process_name="") if w["hwnd"] == hwnd]
        if not matches:
            raise WindowCaptureError(f"找不到 hwnd={hwnd} 的可見視窗。")
        window = matches[0]

    if window["minimized"]:
        notes.append("視窗目前為最小化狀態。")
        if restore_if_minimized:
            ctypes.windll.user32.ShowWindow(window["hwnd"], SW_RESTORE)
            notes.append("已還原視窗以便擷取。")
            window = next(
                (w for w in find_windows(process_name="") if w["hwnd"] == window["hwnd"]),
                window,
            )

    image = None
    method = "PrintWindow"
    try:
        image = _capture_print_window(window["hwnd"])
    except Exception as exc:  # pragma: no cover - 取決於系統
        notes.append(f"PrintWindow 發生例外：{type(exc).__name__}: {exc}")
        image = None

    if image is None:
        notes.append("PrintWindow 失敗，改用 ImageGrab（需要視窗實際顯示在螢幕上）。")
    elif _looks_blank(image):
        notes.append("PrintWindow 取得的畫面幾乎空白，改用 ImageGrab 重試。")
        image = None

    if image is None:
        method = "ImageGrab"
        try:
            image = _capture_image_grab(window["rect"])
        except Exception as exc:
            raise WindowCaptureError(
                f"PrintWindow 與 ImageGrab 都失敗：{type(exc).__name__}: {exc}"
            ) from exc
        if _looks_blank(image):
            notes.append(
                "ImageGrab 取得的畫面也幾乎空白，視窗可能被完全遮住或已最小化。"
            )

    return {
        "image": image,
        "method": method,
        "window": {k: v for k, v in window.items()},
        "notes": notes,
    }
