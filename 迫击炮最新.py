"""
PUBG 迫击炮自动测距 - 原版实战极简修正版
说明：准心已锁定(303, 303)，比例锁定82.96，排版锁定。即开即用，不挡视野。
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
        # 丢弃第一帧防缓存
        sct.grab(monitor) 
        shot = sct.grab(monitor)
        return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)

def find_all_colors_points(img_bgr):
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    
    # 修正橙色色相为 7~14，且 S/V 设为 210 直接过滤轰炸区暗红背景
    ranges = {
        "orange": [(np.array([7, 210, 210]), np.array([14, 255, 255]))],
        "yellow": [(np.array([20, 200, 200]), np.array([35, 255, 255]))],
        "green":  [(np.array([45, 200, 200]), np.array([85, 255, 255]))],
        "blue":   [(np.array([100, 200, 200]), np.array([130, 255, 255]))]
    }

    results = {}

    for color_name, rng_list in ranges.items():
        mask = None
        for low, high in rng_list:
            m = cv2.inRange(hsv, low, high)
            mask = m if mask is None else cv2.bitwise_or(mask, m)
        
        # 闭运算填补中间白色的数字空洞
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        max_area = 0
        best_pt = None
        best_rect = None
        
        for c in contours:
            area = cv2.contourArea(c)
            # 水滴标点面积约 85~250，限制在 70~450 之间防噪点和大块背景
            if 70 < area < 450: 
                x, y, w, h = cv2.boundingRect(c)
                
                # 水滴标点必然是竖长形 (高 > 宽)，直接过滤正方形/横向的队友位置图标
                if h <= w or (h / float(w)) > 1.8:
                    continue

                pt_x = x + w // 2
                pt_y = y + h  # 底部尖端

                dist_to_center = math.hypot(pt_x - CENTER_X, pt_y - CENTER_Y)
                if dist_to_center <= 30:
                    continue
                    
                if area > max_area:
                    max_area = area
                    best_pt = (pt_x, pt_y)
                    best_rect = (x, y, w, h)
                    
        if best_pt:
            results[color_name] = {"pt": best_pt, "rect": best_rect}

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