"""
PUBG 迫击炮自动测距 - 终极抗轰炸区版 (颜色 + 内部黑底白字 1/2/3/4 + 水滴尖角 三重锁定)
说明：
1. 彻底解决轰炸区(红区)与橙色标点融为一体、或误框红区空地的问题。
2. 必须同时满足：【外圈对应颜色】+【内部黑底圆心】+【中心白色数字特征】+【底部水滴尖角】才会识别。
"""
import ctypes
import math
import threading
import time
import tkinter as tk
from tkinter import messagebox
import cv2
import numpy as np
import mss
import os
import datetime

# 强制开启高 DPI 感知
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
HOTKEY_VK = 0x70  # F1 键

# ======== 核心配置（写死） ========
FIXED_REGION = (1906, 790, 610, 611)   # 地图截图区域
DEFAULT_PX_PER_100M = 82.96            # 校准好的比例

# 锁定准心
CENTER_X = 303  
CENTER_Y = 303  

# 锁定 UI 排版
UI_BASE_X = 1910
UI_BASE_Y = 740
UI_FONT_SIZE = 15
UI_GAP = 35
UI_BOX_W = 120
UI_BOX_H = 50

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(APP_DIR, "distance_log.txt")

def write_log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{now}] {msg}\n")
    except Exception:
        pass

def grab_region():
    x, y, w, h = FIXED_REGION
    with mss.mss() as sct:
        monitor = {"top": y, "left": x, "width": w, "height": h}
        sct.grab(monitor) 
        shot = sct.grab(monitor)
        return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)

def build_digit_templates():
    """
    动态生成 PUBG 标点中心 1, 2, 3, 4 的标准二值化小模板 (尺寸 9x11)
    用于在黑底圆心中校验数字形状（特别是强化数字 2 的匹配）
    """
    templates = {}
    font = cv2.FONT_HERSHEY_SIMPLEX
    for d in [1, 2, 3, 4]:
        canvas = np.zeros((16, 16), dtype=np.uint8)
        cv2.putText(canvas, str(d), (2, 13), font, 0.45, 255, 1, cv2.LINE_AA)
        _, bw = cv2.threshold(canvas, 80, 255, cv2.THRESH_BINARY)
        coords = cv2.findNonZero(bw)
        if coords is not None:
            x, y, w, h = cv2.boundingRect(coords)
            templates[d] = cv2.resize(bw[y:y+h, x:x+w], (9, 11))
    return templates

DIGIT_TEMPLATES = build_digit_templates()

def find_all_colors_points(img_bgr):
    """
    核心算法：以【黑底圆心 + 白色数字】为锚点，向外探测【水滴彩色外框】
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    H_img, W_img = gray.shape

    # 1. 定义四色标点的 HSV 范围及对应队友编号
    color_specs = {
        "yellow": {"digit": 1, "ranges": [(np.array([18, 130, 140]), np.array([36, 255, 255]))]},
        "orange": {"digit": 2, "ranges": [(np.array([4, 150, 150]),  np.array([17, 255, 255]))]},
        "blue":   {"digit": 3, "ranges": [(np.array([95, 130, 140]), np.array([132, 255, 255]))]},
        "green":  {"digit": 4, "ranges": [(np.array([40, 130, 140]), np.array([88, 255, 255]))]}
    }

    color_masks = {}
    for cname, spec in color_specs.items():
        m_total = None
        for low, high in spec["ranges"]:
            m = cv2.inRange(hsv, low, high)
            m_total = m if m_total is None else cv2.bitwise_or(m_total, m)
        color_masks[cname] = m_total

    # 2. 提取标点内部的【深黑色圆心背景】(亮度 V < 85 或 灰度 < 80)
    # 即使在红区，2号标点数字背后的黑底依然是深暗色，能将标点从大片红区中独立切割出来！
    dark_mask = cv2.inRange(gray, 0, 88)

    # 3. 提取标点中心【高亮白色/浅色数字】(低饱和度 + 高亮度)
    white_digit_mask = cv2.inRange(hsv, np.array([0, 0, 155]), np.array([180, 110, 255]))

    # 数字必须在黑色圆心内部或紧邻黑底：对黑底做轻微膨胀后与白字取交集
    kernel_3 = np.ones((3, 3), np.uint8)
    dark_dilated = cv2.dilate(dark_mask, kernel_3, iterations=2)
    valid_white = cv2.bitwise_and(white_digit_mask, dark_dilated)

    # 寻找所有候选白色数字连通域
    num_contours, _ = cv2.findContours(valid_white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    results = {}
    best_scores = {"yellow": -1.0, "orange": -1.0, "blue": -1.0, "green": -1.0}

    for c in num_contours:
        x, y, w, h = cv2.boundingRect(c)
        # PUBG 小地图标点内的数字尺寸极其固定：宽 2~11px，高 5~14px
        if not (2 <= w <= 12 and 5 <= h <= 15):
            continue

        # 数字中心点坐标 (cx, cy) —— 即水滴标点圆头的中心
        cx = x + w // 2
        cy = y + h // 2

        # 排除屏幕正中心准心 30px 范围与边缘越界区域
        if math.hypot(cx - CENTER_X, cy - CENTER_Y) <= 30:
            continue
        if cx < 15 or cx > W_img - 15 or cy < 15 or cy > H_img - 22:
            continue

        # 校验1：数字周围半径 5px 内必须有充足的黑色圆心像素（过滤地图上的白色文字如 "Terminal"）
        roi_dark = dark_mask[cy-6:cy+7, cx-6:cx+7]
        dark_count = cv2.countNonZero(roi_dark)
        if dark_count < 25:  # 没有黑底包裹的白字（如地图英文地名、网格白线）直接淘汰！
            continue

        # 提取当前数字切片并归一化为 9x11，用于跟 1, 2, 3, 4 模板比对
        digit_Patch = gray[y:y+h, x:x+w]
        _, digit_bw = cv2.threshold(digit_Patch, 140, 255, cv2.THRESH_BINARY)
        digit_resized = cv2.resize(digit_bw, (9, 11))

        # 遍历四种颜色，检查该“黑底白字”外围是否包裹着对应颜色的水滴框
        for cname, spec in color_specs.items():
            cmask = color_masks[cname]
            target_d = spec["digit"]

            # 校验2：水滴圆头外环（距离中心 5~10px 的上半圆环）必须有高比例的目标颜色
            # 采样上、左、右三个方向的彩色包边（避开下方，因为下方要单独测尖角）
            top_color = cv2.countNonZero(cmask[cy-11:cy-5, cx-6:cx+7])     # 上方弧顶 (6x13 = 78px)
            left_color = cv2.countNonZero(cmask[cy-5:cy+5, cx-11:cx-5])    # 左侧弧边 (10x6 = 60px)
            right_color = cv2.countNonZero(cmask[cy-5:cy+5, cx+6:cx+12])   # 右侧弧边 (10x6 = 60px)

            ring_fill = (top_color + left_color + right_color) / 198.0
            if ring_fill < 0.35:  # 外圈颜色不匹配
                continue

            # 校验3：必须具备【水滴下方尖角】且【区别于队友圆形/方形图标】
            # 水滴标点在中心下方 (cy+8 到 cy+17, 窄带 cx-4 到 cx+5) 是实心彩色尖端
            tip_zone = cmask[cy+7:cy+17, cx-4:cx+5]  # 10x9 = 90px
            tip_color = cv2.countNonZero(tip_zone)
            if tip_color < 18:  # 下方没有延伸出的彩色水滴尖角
                continue

            # 校验4：区分水滴与轰炸区背景/方形图标
            # 水滴标点在圆心内部 (cx-4:cx+5, cy-4:cy+5) 是黑底白字，因此彩色像素极少！
            inner_color = cv2.countNonZero(cmask[cy-4:cy+5, cx-4:cx+5])
            if inner_color > 35:  # 内部全是彩色说明不是空心黑底标点
                continue

            # 水滴下半部两侧 (cy+10:cy+17 的左下方和右下方) 应该向中间收尖，不应全是黑色边框
            # 进一步校验在 cy+6 ~ cy+18 每一行彩色像素的水平跨度是否逐渐变窄（收尖特征）
            widths = []
            for row_y in range(cy + 5, min(H_img, cy + 17)):
                row_slice = cmask[row_y, cx-9:cx+10]
                nz = np.where(row_slice > 0)[0]
                if len(nz) >= 2:
                    widths.append(nz[-1] - nz[0] + 1)
                elif len(nz) == 1:
                    widths.append(1)

            if len(widths) < 4:
                continue

            # 找到水滴底部尖端的精确 Y 坐标（从中心往下，仍然紧贴黑色外描边或彩色尖端的最低点）
            tip_y = cy + 14  # 标准水滴中心到底部尖端距离约为 14~15 像素
            for dy in range(17, 9, -1):
                if cy + dy < H_img and np.any(cmask[cy + dy, cx-3:cx+4] > 0):
                    tip_y = cy + dy
                    break

            # 校验5：数字模板匹配得分（特别是校验数字 "2" 等的笔画吻合度）
            tmpl = DIGIT_TEMPLATES[target_d]
            match_res = cv2.matchTemplate(digit_resized, tmpl, cv2.TM_CCOEFF_NORMED)
            digit_score = float(match_res[0, 0])

            # 对于数字 2 (orange)，额外验证数字 "2" 的结构特征：底部横线宽、中上部有开口
            if cname == "orange":
                # 数字 2 的宽高比通常在 0.45 ~ 0.90 之间（区别于数字 1 的极窄）
                if (w / float(h)) < 0.42:
                    continue
                if digit_score < 0.15:
                    continue

            # 综合得分 = 外圈颜色饱满度 + 黑底对比度 + 水滴尖角饱满度 + 数字形状匹配分
            total_score = (ring_fill * 100.0) + (dark_count * 1.5) + (tip_color * 1.2) + (digit_score * 80.0)

            if total_score > best_scores[cname]:
                best_scores[cname] = total_score
                # 锁定水滴外框与底部尖端精确坐标
                box_x = cx - 11
                box_y = cy - 11
                box_w = 22
                box_h = (tip_y - box_y) + 1
                results[cname] = {
                    "pt": (cx, tip_y),
                    "rect": (box_x, box_y, box_w, box_h)
                }

    return results

class ResultDisplay:
    """只显示数字的透明窗口"""
    def __init__(self, master, text, color_hex, x, y):
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        
        self.transparent_color = "#010203"
        self.top.configure(bg=self.transparent_color)
        self.top.attributes("-transparentcolor", self.transparent_color)
        
        self.top.geometry(f"{UI_BOX_W}x{UI_BOX_H}+{x}+{y}")
        
        font_style = ("Microsoft YaHei", UI_FONT_SIZE, "bold")
        self.canvas = tk.Canvas(self.top, bg=self.transparent_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.canvas.create_text(UI_BOX_W//2 + 1, UI_BOX_H//2 + 1, text=text, fill="black", font=font_style, anchor="center")
        self.canvas.create_text(UI_BOX_W//2, UI_BOX_H//2, text=text, fill=color_hex, font=font_style, anchor="center")

        self.set_click_through()
        self.top.after(3000, self.close)

    def set_click_through(self):
        try:
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id())
            styles = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            styles = styles | WS_EX_LAYERED | WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, styles)
        except Exception:
            pass

    def close(self):
        try:
            if self.top.winfo_exists():
                self.top.destroy()
        except Exception:
            pass

class DebugOverlay:
    """直接在小地图原位画框的透明层"""
    def __init__(self, master, targets):
        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        
        self.transparent_color = "#010203"
        self.top.configure(bg=self.transparent_color)
        self.top.attributes("-transparentcolor", self.transparent_color)
        
        x, y, w, h = FIXED_REGION
        self.top.geometry(f"{w}x{h}+{x}+{y}")
        
        self.canvas = tk.Canvas(self.top, bg=self.transparent_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        color_map = {
            "orange": "#FF4500",
            "yellow": "#FFD700",
            "green": "#32CD32",
            "blue": "#00BFFF"
        }

        # 画准心 (303, 303)
        self.canvas.create_line(CENTER_X-15, CENTER_Y, CENTER_X+15, CENTER_Y, fill="white", width=2)
        self.canvas.create_line(CENTER_X, CENTER_Y-15, CENTER_X, CENTER_Y+15, fill="white", width=2)
        self.canvas.create_oval(CENTER_X-3, CENTER_Y-3, CENTER_X+3, CENTER_Y+3, outline="white", width=1)

        for color_key, data in targets.items():
            rect = data["rect"]
            c_hex = color_map.get(color_key, "white")
            self.canvas.create_rectangle(rect[0], rect[1], rect[0]+rect[2], rect[1]+rect[3], outline=c_hex, width=3)
            self.canvas.create_text(rect[0]+rect[2]//2, rect[1]-10, text=color_key, fill=c_hex, font=("Arial", 12, "bold"))

        self.set_click_through()
        self.top.after(1500, self.close)

    def set_click_through(self):
        try:
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id())
            styles = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            styles = styles | WS_EX_LAYERED | WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, styles)
        except Exception:
            pass

    def close(self):
        try:
            if self.top.winfo_exists():
                self.top.destroy()
        except Exception:
            pass


class MainApp:
    def __init__(self, root):
        self.root = root
        self.root.title("迫击炮测距")
        self.root.geometry("260x150")
        self.root.attributes("-topmost", True)
        self.active_windows = []
        self.is_capturing = False
        
        tk.Label(self.root, text="程序已在后台待命", fg="green", font=("Microsoft YaHei", 12, "bold")).pack(pady=10)
        tk.Label(self.root, text="游戏中按 N 开图，按 F1 测距", fg="gray").pack()
        tk.Button(self.root, text="查看日志", command=self.open_log, font=("Arial", 9)).pack(pady=15)
        
        threading.Thread(target=self.hotkey_listener, daemon=True).start()

    def open_log(self):
        if os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo("提示", "还没有日志，先去游戏里按 F1 测一下！")

    def hotkey_listener(self):
        prev = False
        while True:
            down = (user32.GetAsyncKeyState(HOTKEY_VK) & 0x8000) != 0
            if down and not prev:
                self.trigger_capture()
            prev = down
            time.sleep(0.02)

    def trigger_capture(self):
        if self.is_capturing:
            return
        self.is_capturing = True
        threading.Thread(target=self._capture_task, daemon=True).start()

    def _capture_task(self):
        try:
            img = grab_region()
            targets = find_all_colors_points(img)
            self.root.after(0, self.process_results, targets)
        except Exception as e:
            write_log(f"截图识别出错: {e}")
        finally:
            time.sleep(0.1)
            self.is_capturing = False

    def process_results(self, targets):
        for w in self.active_windows:
            w.close()
        self.active_windows.clear()

        # 弹出原位透视框
        debug_win = DebugOverlay(self.root, targets)
        self.active_windows.append(debug_win)

        color_configs = [
            ("yellow", "黄", "#FFD700"),
            ("orange", "橙", "#FF4500"),  
            ("blue", "蓝", "#00BFFF"),
            ("green", "绿", "#32CD32")
        ]

        log_msgs = []

        for i, (color_key, prefix, hex_color) in enumerate(color_configs):
            current_x = UI_BASE_X + i * (UI_BOX_W + UI_GAP)
            
            if color_key in targets:
                pt = targets[color_key]["pt"]
                dist_px = math.hypot(pt[0] - CENTER_X, pt[1] - CENTER_Y)
                real_dist_m = (dist_px / DEFAULT_PX_PER_100M) * 100
                text = f"{prefix} {real_dist_m:.0f}米"
                log_msgs.append(f"{prefix}:{real_dist_m:.1f}m")
            else:
                text = f"{prefix} 无标点"
                log_msgs.append(f"{prefix}:无")

            win = ResultDisplay(self.root, text, hex_color, current_x, UI_BASE_Y)
            self.active_windows.append(win)

        write_log(f"结果: {' | '.join(log_msgs)}")

if __name__ == "__main__":
    root = tk.Tk()
    app = MainApp(root)
    root.mainloop()