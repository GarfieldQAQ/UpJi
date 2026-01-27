import sys
import os
import struct
import time
import traceback
import serial
import serial.tools.list_ports
import numpy as np
import pyqtgraph as pg
from collections import deque
from datetime import datetime

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QGridLayout, QLabel, QDoubleSpinBox, QSpinBox, 
                               QFrame, QPushButton, QSplitter, QComboBox, QDialog, 
                               QSizePolicy, QGroupBox, QMessageBox, QSizeGrip, QTextEdit,
                               QTabWidget)
from PySide6.QtCore import (Qt, QTimer, QPropertyAnimation, QEasingCurve, Property, 
                            QPoint, Signal, QThread, Slot, QObject, QSize, QRect)
from PySide6.QtGui import QColor, QPainter, QFont, QIcon, QAction, QRegion, QBitmap, QBrush, QPen

# -----------------------------------------------------------------------------
# 1. 全局配置
# -----------------------------------------------------------------------------
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
pg.setConfigOptions(antialias=False)
# 为了稳定，暂时禁用OpenGL
pg.setConfigOption('useOpenGL', False) 

# =============================================================================
# 2. UI 组件 (LED, 标题栏, 开关)
# =============================================================================
class LedIndicator(QWidget):
    def __init__(self, color="#00FF00", parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.base_color = QColor(color)
        self.current_color = QColor("#222222") 
        self.is_on = False
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.turn_off)

    def flash(self):
        self.current_color = self.base_color; self.is_on = True; self.update(); self.timer.start(50)
    def turn_off(self):
        self.current_color = QColor("#333333"); self.is_on = False; self.update()
    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        if self.is_on:
            p.setBrush(QBrush(self.base_color.lighter(150))); p.setPen(Qt.NoPen); p.drawEllipse(0, 0, 16, 16)
        p.setBrush(QBrush(self.current_color)); p.setPen(QPen(QColor("#111"))); p.drawEllipse(2, 2, 12, 12)

class CustomTitleBar(QWidget):
    def __init__(self, parent=None, title="Window"):
        super().__init__(parent)
        self.setFixedHeight(35)
        self.setStyleSheet("background-color: #1e1e1e; border-bottom: 1px solid #333; border-top-left-radius: 8px; border-top-right-radius: 8px;")
        layout = QHBoxLayout(self); layout.setContentsMargins(10, 0, 0, 0); layout.setSpacing(0)
        self.lbl_title = QLabel(title); self.lbl_title.setStyleSheet("color: #ccc; font-weight: bold; border:none;")
        layout.addWidget(self.lbl_title); layout.addStretch()
        
        btn_css = "QPushButton { background: transparent; border: none; color: #aaa; font-size: 14px; } QPushButton:hover { background: #333; color: white; }"
        cls_css = "QPushButton { background: transparent; border: none; color: #aaa; font-size: 14px; border-top-right-radius: 8px; } QPushButton:hover { background: #e81123; color: white; }"
        
        btn_min = QPushButton("─"); btn_min.setFixedSize(45,35); btn_min.setStyleSheet(btn_css); btn_min.clicked.connect(self.window().showMinimized)
        btn_max = QPushButton("□"); btn_max.setFixedSize(45,35); btn_max.setStyleSheet(btn_css); btn_max.clicked.connect(self.toggle_max)
        btn_cls = QPushButton("✕"); btn_cls.setFixedSize(45,35); btn_cls.setStyleSheet(cls_css); btn_cls.clicked.connect(self.window().close)
        layout.addWidget(btn_min); layout.addWidget(btn_max); layout.addWidget(btn_cls)
        self.btn_max = btn_max

    def toggle_max(self):
        win = self.window()
        if win.isMaximized(): win.showNormal(); self.btn_max.setText("□")
        else: win.showMaximized(); self.btn_max.setText("❐")
    def mousePressEvent(self, e): 
        if e.button() == Qt.LeftButton: self.start_pos = e.globalPosition().toPoint()
    def mouseMoveEvent(self, e):
        if hasattr(self, 'start_pos') and self.start_pos: 
            self.window().move(self.window().pos() + e.globalPosition().toPoint() - self.start_pos)
            self.start_pos = e.globalPosition().toPoint()
    def mouseReleaseEvent(self, e): self.start_pos = None
    def mouseDoubleClickEvent(self, e): self.toggle_max()

class AnimatedToggle(QWidget):
    def __init__(self, parent=None, active_color="#00E5FF"):
        super().__init__(parent)
        self.setFixedSize(50, 28); self.setCursor(Qt.PointingHandCursor)
        self._checked = False; self._handle_position = 3
        self._active_color = QColor(active_color); self._bg_color = QColor("#424242")
        self.animation = QPropertyAnimation(self, b"handle_position", self)
        self.animation.setEasingCurve(QEasingCurve.InOutCubic); self.animation.setDuration(200)
    @Property(float)
    def handle_position(self): return self._handle_position
    @handle_position.setter
    def handle_position(self, pos): self._handle_position = pos; self.update()
    def isChecked(self): return self._checked
    def mouseReleaseEvent(self, e):
        self._checked = not self._checked
        self.animation.setEndValue(self.width() - 25 if self._checked else 3); self.animation.start()
        super().mouseReleaseEvent(e)
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(self._active_color if self._checked else self._bg_color); p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, self.width(), self.height(), 14, 14)
        p.setBrush(QColor("#FFF")); p.drawEllipse(QPoint(int(self._handle_position) + 11, 14), 11, 11)

# =============================================================================
# 3. 串口逻辑 (可靠传输 + 20次重试 + 状态反馈)
# =============================================================================
class SerialWorker(QThread):
    data_received = Signal(list)      
    log_message = Signal(str, str)    
    tx_flash = Signal()               
    rx_flash = Signal()               
    ack_received = Signal(int, bool)  # int=ID, bool=Success/Fail
    
    def __init__(self, port_name, baud_rate):
        super().__init__()
        self.port_name = port_name; self.baud_rate = baud_rate
        self.is_running = True; self.ser = None
        self.pending_cmd = None; self.pending_channel = -1; self.last_send_time = 0
        self.retry_count = 0; 
        self.MAX_RETRIES = 20;  # 重试20次
        self.RETRY_INTERVAL = 0.2 

    def run(self):
        try:
            self.ser = serial.Serial(self.port_name, self.baud_rate, timeout=0.01)
            self.log_message.emit(f"Connected to {self.port_name}", "#00FF00")
        except Exception as e:
            self.log_message.emit(f"Connection Failed: {e}", "#FF0000"); return

        TAIL = b'\x00\x00\x80\x7f'; FRAME_LEN = 24; buffer = b''

        while self.is_running and self.ser.is_open:
            # 1. 重传逻辑
            if self.pending_cmd:
                now = time.time()
                if now - self.last_send_time > self.RETRY_INTERVAL:
                    if self.retry_count < self.MAX_RETRIES:
                        try:
                            self.ser.write(self.pending_cmd); self.last_send_time = now
                            self.retry_count += 1; self.tx_flash.emit() 
                            if self.retry_count > 1: self.log_message.emit(f"Retry {self.retry_count}/{self.MAX_RETRIES}...", "#FFA500")
                        except: pass
                    else:
                        # 超过20次，发送失败
                        self.log_message.emit("Error: Timeout (Give up)", "#FF0000")
                        self.ack_received.emit(self.pending_channel, False) # False = 失败
                        self.pending_cmd = None
            
            # 2. 接收逻辑
            try:
                if self.ser.in_waiting:
                    chunk = self.ser.read(self.ser.in_waiting); buffer += chunk
                    if len(chunk) > 0: self.rx_flash.emit()

                    if b'Success' in buffer:
                        self.log_message.emit("Rx: Success", "#00FF00")
                        buffer = buffer.replace(b'Success', b'') 
                        if self.pending_cmd:
                            self.pending_cmd = None
                            self.ack_received.emit(self.pending_channel, True) # True = 成功

                    while len(buffer) >= 8: 
                        tail_idx = buffer.find(TAIL)
                        if tail_idx == -1:
                            if len(buffer) > 200: buffer = buffer[-50:]
                            break
                        pre_tail_len = tail_idx; valid_len = min(pre_tail_len, FRAME_LEN); valid_len -= (valid_len % 4)
                        if valid_len > 0:
                            packet = buffer[tail_idx - valid_len : tail_idx]
                            try: floats = struct.unpack(f'<{valid_len//4}f', packet); self.data_received.emit(list(floats))
                            except: pass
                        buffer = buffer[tail_idx + 4:]
            except Exception as e: self.log_message.emit(f"IO Error: {e}", "#FF0000"); break
            self.msleep(5) 
        if self.ser: self.ser.close()

    def send_command_reliable(self, cmd_str, channel_id):
        if self.ser and self.ser.is_open:
            if self.pending_cmd is not None: self.log_message.emit("Busy...", "#FFA500")
            
            # 显示蓝色 TX 日志
            self.log_message.emit(f"TX >> {cmd_str}", "#448AFF") 
            self.pending_cmd = cmd_str.encode('utf-8'); self.pending_channel = channel_id
            self.retry_count = 0; self.last_send_time = 0 
        else: 
            self.log_message.emit("Error: Not Connected", "#FF0000")
            self.ack_received.emit(channel_id, False) # 未连接直接视为失败
    
    def stop(self): self.is_running = False; self.wait()

# =============================================================================
# 4. 绘图相关组件
# =============================================================================
class PlaceholderWidget(QFrame):
    def __init__(self, channel_id, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel); self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("QFrame { background-color: #161616; border: 2px dashed #333; border-radius: 8px; } QLabel { color: #444; font-weight: bold; font-size: 14px; }")
        layout = QVBoxLayout(self); layout.setAlignment(Qt.AlignCenter)
        layout.addWidget(QLabel("↗", styleSheet="font-size: 30px; color: #333;")); layout.addWidget(QLabel(f"CH {channel_id} Detached"))

class DetachedWindow(QDialog):
    window_closed = Signal(int)
    def __init__(self, channel_id, content_widget, parent=None):
        super().__init__(parent)
        self.channel_id = channel_id; self.content_widget = content_widget
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground) 
        self.resize(800, 500)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        self.container = QFrame(); self.container.setStyleSheet("QFrame { background-color: #121212; border: 1px solid #444; border-radius: 8px; }")
        c_layout = QVBoxLayout(self.container); c_layout.setContentsMargins(0, 0, 0, 0); c_layout.setSpacing(0)
        self.title_bar = CustomTitleBar(self, title=f"Channel {channel_id} - Monitor")
        c_layout.addWidget(self.title_bar)
        content_area = QWidget(); content_area.setStyleSheet("border: none; border-radius: 0px;") 
        c_layout.addWidget(self.content_widget)
        layout.addWidget(self.container)
        self.grip = QSizeGrip(self); self.grip.setStyleSheet("background: transparent; width: 20px; height: 20px;")
    def resizeEvent(self, event): rect = self.rect(); self.grip.move(rect.right() - 20, rect.bottom() - 20); super().resizeEvent(event)
    def closeEvent(self, event): self.content_widget.setParent(None); self.window_closed.emit(self.channel_id); super().closeEvent(event)

class SmartPlotWidget(QFrame):
    pop_out_req = Signal(int)
    def __init__(self, channel_id, color_hex, parent=None):
        super().__init__(parent)
        self.channel_id = channel_id; self.color = color_hex
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(f"background-color: #1e1e1e; border: 1px solid #333; border-radius: 8px;")
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        header = QWidget(); header.setFixedHeight(32); header.setStyleSheet("background-color: #252525; border-bottom: 1px solid #333; border-top-left-radius: 8px; border-top-right-radius: 8px;")
        h_layout = QHBoxLayout(header); h_layout.setContentsMargins(8, 0, 8, 0)
        title = QLabel(f"CH {channel_id}", styleSheet=f"color: {self.color}; font-weight: bold; border: none;")
        self.combo_time = QComboBox(); self.combo_time.addItems(["50ms", "100ms", "500ms", "1s", "2s", "5s", "10s", "All"])
        self.combo_time.setCurrentText("2s"); self.combo_time.setFixedWidth(70)
        self.combo_time.setStyleSheet("background: #333; color: white; border: none; padding-left:4px; border-radius: 4px;")
        self.combo_time.currentTextChanged.connect(self.update_x_range)
        btn_auto = QPushButton("Fit"); btn_auto.setFixedSize(40, 20); btn_auto.setCursor(Qt.PointingHandCursor)
        btn_auto.setStyleSheet("QPushButton { background: #444; color: white; border: none; border-radius: 4px; } QPushButton:hover { background: #555; }")
        btn_auto.clicked.connect(self.auto_scale)
        self.btn_pop = QPushButton("↗"); self.btn_pop.setFixedSize(25, 20); self.btn_pop.setCursor(Qt.PointingHandCursor)
        self.btn_pop.setStyleSheet("QPushButton { background: #444; color: #00E5FF; border: none; font-weight: bold; border-radius: 4px;} QPushButton:hover { background: #555; }")
        self.btn_pop.clicked.connect(self.request_pop_out)
        h_layout.addWidget(title); h_layout.addSpacing(10); h_layout.addWidget(self.combo_time)
        h_layout.addStretch(); h_layout.addWidget(btn_auto); h_layout.addWidget(self.btn_pop)
        self.plot_item = pg.PlotWidget(); self.plot_item.setBackground('#1e1e1e'); self.plot_item.showGrid(x=True, y=True, alpha=0.2); self.plot_item.setMouseEnabled(x=True, y=True); self.plot_item.hideButtons()
        self.plot_item.setClipToView(True); self.plot_item.setDownsampling(auto=False); self.plot_item.getPlotItem().layout.setContentsMargins(0, 5, 0, 0)
        self.curve = self.plot_item.plot(pen=pg.mkPen(color=self.color, width=1), skipFiniteCheck=True)
        self.target_line = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(color='#FFFFFF', style=Qt.DashLine, width=1, alpha=80))
        self.plot_item.addItem(self.target_line); layout.addWidget(header); layout.addWidget(self.plot_item)
        self.raw_t = None; self.raw_data = None; self.plot_item.sigXRangeChanged.connect(self.refresh_view)
        self.update_x_range(self.combo_time.currentText())

    def update_x_range(self, text):
        if text == "All": self.plot_item.enableAutoRange(axis='x')
        else:
            self.plot_item.disableAutoRange(axis='x'); val = float(text.replace('s', '').replace('m', ''))
            if 'm' in text: val /= 1000.0
            self.plot_item.setXRange(-val, 0, padding=0)
    def auto_scale(self): self.plot_item.enableAutoRange(axis='y')
    def request_pop_out(self): self.pop_out_req.emit(self.channel_id)
    def update_data(self, t_axis, data_array, target_val): self.raw_t = t_axis; self.raw_data = data_array; self.target_line.setPos(target_val); self.refresh_view()
    def refresh_view(self):
        if self.raw_t is None or self.raw_data is None: return
        view = self.plot_item.viewRange()[0]; x_min, x_max = view
        idx_start = np.searchsorted(self.raw_t, x_min); idx_end = np.searchsorted(self.raw_t, x_max)
        if idx_start > 0: idx_start -= 1
        if idx_end < len(self.raw_t): idx_end += 1
        if idx_end <= idx_start: return
        t_view = self.raw_t[idx_start:idx_end]; data_view = self.raw_data[idx_start:idx_end]
        TARGET_POINTS = 3000 
        if len(data_view) > TARGET_POINTS:
            step = len(data_view) // TARGET_POINTS; 
            if step < 1: step = 1
            self.curve.setData(t_view[::step], data_view[::step])
        else: self.curve.setData(t_view, data_view)

# =============================================================================
# 5. 主程序 MainWindow
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(1400, 950)
        
        self.fs = 1000.0; self.max_history = 20.0 
        self.reset_buffers()
        self.colors = ['#FF5252', '#448AFF', '#69F0AE', '#E040FB', '#FFD740', '#00E5FF']
        
        # 记录上一次成功的数值，用于失败回滚
        self.last_confirmed_values = {i: 0.0 for i in range(6)}
    
        self.is_test_mode = False; self.serial_thread = None; self.latest_serial_data = [0.0] * 6 
        self.is_paused = False

        self.init_ui()
        self.refresh_ports()
        self.timer = QTimer(); self.timer.timeout.connect(self.on_timer_tick); self.timer.start(30) 

    def reset_buffers(self):
        self.buffer_len = int(self.fs * self.max_history)
        self.data_buffer = np.zeros((6, self.buffer_len), dtype=np.float32)
        self.t_axis = np.linspace(-self.max_history, 0, self.buffer_len, dtype=np.float32)
        self.global_ptr = 0

    def init_ui(self):
        root_widget = QFrame(); root_widget.setObjectName("Root")
        root_widget.setStyleSheet("QFrame#Root { background-color: #121212; border: 1px solid #444; border-radius: 8px; }")
        self.setCentralWidget(root_widget)
        root_layout = QVBoxLayout(root_widget); root_layout.setContentsMargins(0, 0, 0, 0); root_layout.setSpacing(0)
        self.title_bar = CustomTitleBar(self, title="JustFloat Oscilloscope Pro v10.0 (Tab+Button+Reliable)")
        root_layout.addWidget(self.title_bar)

        content_widget = QWidget(); main_layout = QHBoxLayout(content_widget); main_layout.setContentsMargins(15, 15, 15, 15)

        # Left Panel
        left_panel = QWidget(); left_layout = QVBoxLayout(left_panel); left_layout.setContentsMargins(0,0,0,0)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #3d3d3d; border-radius: 6px; background: #2b2b2b; }
            QTabBar::tab { background: #2b2b2b; color: #888; padding: 8px 15px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
            QTabBar::tab:selected { background: #3d3d3d; color: #00E5FF; font-weight: bold; border-bottom: 2px solid #00E5FF; }
            QScrollBar:vertical { border: none; background: #1a1a1a; width: 8px; border-radius: 4px; }
            QScrollBar::handle:vertical { background: #555; border-radius: 4px; }
        """)
        
        # TAB 1: Controls
        tab_ctrl = QWidget(); tc_layout = QVBoxLayout(tab_ctrl); tc_layout.setSpacing(10); tc_layout.setContentsMargins(10, 15, 10, 15)
        
        grp_conn = QGroupBox("CONNECTION"); grp_conn.setStyleSheet("QGroupBox{font-weight:bold; color:#888; border:1px solid #444; border-radius:6px; margin-top:10px;} QGroupBox::title{subcontrol-origin:margin; left:10px; padding:0 3px;}")
        gl = QVBoxLayout(grp_conn); gl.setSpacing(8)
        
        h_p = QHBoxLayout(); self.combo_port = QComboBox(); btn_ref = QPushButton("↻"); btn_ref.setFixedSize(25,25); btn_ref.clicked.connect(self.refresh_ports)
        self.combo_port.setStyleSheet("background:#2b2b2b; color:white; padding:4px;"); btn_ref.setStyleSheet("background:#333; color:#00E5FF; border:1px solid #555;")
        h_p.addWidget(self.combo_port); h_p.addWidget(btn_ref); gl.addLayout(h_p)
        self.combo_baud = QComboBox(); self.combo_baud.addItems(["9600","115200","921600","2000000"]); self.combo_baud.setCurrentText("115200")
        self.combo_baud.setStyleSheet("background:#2b2b2b; color:white; padding:4px;")
        
        h_led = QHBoxLayout()
        self.led_rx = LedIndicator("#69F0AE"); self.led_tx = LedIndicator("#448AFF")
        h_led.addWidget(QLabel("RX:", styleSheet="color:#888; border:none;")); h_led.addWidget(self.led_rx)
        h_led.addSpacing(15); h_led.addWidget(QLabel("TX:", styleSheet="color:#888; border:none;")); h_led.addWidget(self.led_tx); h_led.addStretch()
        gl.addLayout(h_led)
        
        self.btn_connect = QPushButton("CONNECT"); self.btn_connect.setFixedHeight(30); self.btn_connect.clicked.connect(self.toggle_connection)
        self.btn_connect.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; border-radius: 4px;")
        gl.addWidget(self.combo_baud); gl.addWidget(self.btn_connect); tc_layout.addWidget(grp_conn)
        
        grp_buf = QGroupBox("BUFFER"); grp_buf.setStyleSheet(grp_conn.styleSheet()); bl = QVBoxLayout(grp_buf); bl.setSpacing(8)
        h_b = QHBoxLayout(); self.spin_history = QSpinBox(); self.spin_history.setRange(1, 600); self.spin_history.setValue(int(self.max_history))
        self.spin_history.setStyleSheet("background:#2b2b2b; color:white;"); self.spin_history.valueChanged.connect(self.on_buffer_size_change)
        h_b.addWidget(QLabel("History(s):", styleSheet="color:#ccc; border:none;")); h_b.addWidget(self.spin_history); bl.addLayout(h_b)
        h_act = QHBoxLayout(); self.btn_pause = QPushButton("PAUSE"); self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.setStyleSheet("background:#444; color:white; border-radius:4px;")
        btn_clr = QPushButton("CLEAR"); btn_clr.clicked.connect(self.clear_buffer)
        btn_clr.setStyleSheet("background:#c62828; color:white; border-radius:4px;")
        h_act.addWidget(self.btn_pause); h_act.addWidget(btn_clr); bl.addLayout(h_act); tc_layout.addWidget(grp_buf)
        
        self.btn_test_mode = QPushButton("TEST MODE: OFF"); self.btn_test_mode.setCheckable(True); self.btn_test_mode.clicked.connect(self.toggle_test_mode)
        self.btn_test_mode.setStyleSheet("background:#333; color:#aaa; border:1px solid #555; border-radius:4px; padding:4px;")
        tc_layout.addWidget(self.btn_test_mode)
        
        self.ctrl_widgets = []
        for i in range(6):
            card = self.create_control_card(i); tc_layout.addWidget(card); self.ctrl_widgets.append(card)
        tc_layout.addStretch()

        # TAB 2: Log
        tab_log = QWidget(); tl = QVBoxLayout(tab_log); tl.setContentsMargins(5,5,5,5)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background: #121212; color: #00FF00; font-family: Consolas; font-size: 11px; border:none;")
        tl.addWidget(self.log_view)

        self.tabs.addTab(tab_ctrl, "CONTROL"); self.tabs.addTab(tab_log, "SYSTEM LOG")
        left_layout.addWidget(self.tabs)

        self.plot_container = QWidget(); self.grid_layout = QGridLayout(self.plot_container); self.grid_layout.setSpacing(10); self.grid_layout.setContentsMargins(0,0,0,0)
        self.plot_widgets = {}; self.placeholders = {}; self.detached_windows = {}
        for i in range(6):
            p_widget = SmartPlotWidget(i+1, self.colors[i])
            p_widget.pop_out_req.connect(self.handle_pop_out)
            self.plot_widgets[i+1] = p_widget; row, col = divmod(i, 3); self.grid_layout.addWidget(p_widget, row, col)

        splitter = QSplitter(Qt.Horizontal); splitter.addWidget(left_panel); splitter.addWidget(self.plot_container); splitter.setSizes([340, 1060]); splitter.setHandleWidth(2)
        splitter.setStyleSheet("QSplitter::handle { background-color: #333; }")
        main_layout.addWidget(splitter); root_layout.addWidget(content_widget)
        self.grip = QSizeGrip(self); self.grip.setFixedSize(20, 20)
    
    def resizeEvent(self, event):
        self.grip.move(self.rect().right() - 20, self.rect().bottom() - 20); super().resizeEvent(event)

    def create_control_card(self, idx):
        frame = QFrame(); frame.setStyleSheet(f"background: #1e1e1e; border-radius: 6px; border-left: 4px solid {self.colors[idx]};")
        l = QVBoxLayout(frame); l.setContentsMargins(10, 6, 10, 6); l.setSpacing(4)
        
        h1 = QHBoxLayout(); title = QLabel(f"CH {idx+1}", styleSheet="color: #ddd; font-weight: bold;")
        toggle = AnimatedToggle(active_color=self.colors[idx]); h1.addWidget(title); h1.addStretch(); h1.addWidget(toggle)
        
        h2 = QHBoxLayout()
        spin = QDoubleSpinBox(); spin.setRange(-99999, 99999); spin.setValue(0); spin.setFixedWidth(80)
        spin.setStyleSheet("QDoubleSpinBox { background:#2b2b2b; color:white; border:1px solid #444; padding:2px; } QDoubleSpinBox:disabled { color: #555; border: 1px solid #333; }")
        
        # --- 新增发送按钮 ---
        btn_send = QPushButton("SET")
        btn_send.setFixedSize(40, 24)
        btn_send.setCursor(Qt.PointingHandCursor)
        btn_send.setStyleSheet("""
            QPushButton { background: #444; color: #fff; border: none; border-radius: 4px; font-weight: bold; font-size: 10px; }
            QPushButton:hover { background: #555; }
            QPushButton:pressed { background: #222; }
            QPushButton:disabled { background: #222; color: #555; }
        """)
        btn_send.clicked.connect(lambda: self.send_target_value(idx))
        
        val_lbl = QLabel("0.00"); val_lbl.setFixedWidth(80); val_lbl.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        val_lbl.setStyleSheet(f"color: {self.colors[idx]}; font-family: Consolas; font-weight: bold; font-size: 14px;")
        
        h2.addWidget(QLabel("Ref:", styleSheet="border:none; color:#aaa"))
        h2.addWidget(spin)
        h2.addWidget(btn_send)
        h2.addStretch()
        h2.addWidget(val_lbl)
        
        l.addLayout(h1); l.addLayout(h2)
        frame.toggle = toggle; frame.spin = spin; frame.btn_send = btn_send; frame.val_lbl = val_lbl
        
        # 允许回车触发按钮点击
        spin.editingFinished.connect(btn_send.click)
        return frame

    def send_target_value(self, idx):
        if self.serial_thread and self.serial_thread.isRunning():
            self.setFocus()
            # 锁定 UI
            self.ctrl_widgets[idx].spin.setEnabled(False) 
            self.ctrl_widgets[idx].btn_send.setEnabled(False) 
            
            val = self.ctrl_widgets[idx].spin.value()
            cmd = f"#{val:.2f},valve{idx+1}#"
            self.serial_thread.send_command_reliable(cmd, idx)
        else:
            self.append_log("Warning: Not Connected", "#FFA500")

    def on_ack_received(self, channel_id, is_success):
        if 0 <= channel_id < len(self.ctrl_widgets):
            widget = self.ctrl_widgets[channel_id]
            spin = widget.spin
            btn = widget.btn_send
            
            # 解锁 UI
            spin.setEnabled(True) 
            btn.setEnabled(True)
            
            if is_success:
                # 成功：记录数值
                self.last_confirmed_values[channel_id] = spin.value()
            else:
                # 失败：回滚数值
                old_val = self.last_confirmed_values.get(channel_id, 0.0)
                spin.blockSignals(True) 
                spin.setValue(old_val)
                spin.blockSignals(False)
                self.append_log(f"CH{channel_id+1} Failed: Reverted to {old_val:.2f}", "#FF5252")

    def append_log(self, text, color="#CCCCCC"):
        t_str = datetime.now().strftime("[%H:%M:%S] ")
        self.log_view.append(f'<font color="#555">{t_str}</font><font color="{color}">{text}</font>')
        sb = self.log_view.verticalScrollBar(); sb.setValue(sb.maximum())

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        self.btn_pause.setText("RESUME" if self.is_paused else "PAUSE")
        self.btn_pause.setStyleSheet(f"background:{'#2e7d32' if self.is_paused else '#444'}; color:white; border-radius:4px;")

    def clear_buffer(self):
        self.data_buffer.fill(0)
        for i in range(6):
            target = self.ctrl_widgets[i].spin.value()
            self.plot_widgets[i+1].update_data(self.t_axis, self.data_buffer[i], target)
            self.ctrl_widgets[i].val_lbl.setText("0.00")
        self.append_log("Buffer Cleared", "#888888")

    def on_buffer_size_change(self, val):
        self.max_history = float(val); self.reset_buffers()
    def refresh_ports(self):
        try:
            self.combo_port.clear()
            for p in serial.tools.list_ports.comports(): self.combo_port.addItem(p.device)
        except: pass
    def toggle_test_mode(self):
        self.is_test_mode = self.btn_test_mode.isChecked()
        self.btn_test_mode.setText("TEST MODE: ON" if self.is_test_mode else "TEST MODE: OFF")
        self.btn_test_mode.setStyleSheet(f"background:{'#e65100' if self.is_test_mode else '#333'}; color:{'white' if self.is_test_mode else '#aaa'}; border:1px solid #555; border-radius:4px;")
        if not self.is_test_mode: self.reset_buffers()
        self.append_log(f"Test Mode: {self.is_test_mode}", "#FFA500")

    def toggle_connection(self):
        if self.serial_thread:
            self.serial_thread.stop(); self.serial_thread = None
            self.btn_connect.setText("CONNECT"); self.btn_connect.setStyleSheet("background-color: #2e7d32; color: white; font-weight:bold; border-radius:4px;")
            self.combo_port.setEnabled(True); self.combo_baud.setEnabled(True)
            self.append_log("Disconnected", "#FF0000")
        else:
            if not self.combo_port.currentText(): return
            self.serial_thread = SerialWorker(self.combo_port.currentText(), int(self.combo_baud.currentText()))
            self.serial_thread.data_received.connect(self.on_serial_data)
            self.serial_thread.log_message.connect(self.append_log)
            self.serial_thread.tx_flash.connect(self.led_tx.flash)
            self.serial_thread.rx_flash.connect(self.led_rx.flash)
            self.serial_thread.ack_received.connect(self.on_ack_received)
            self.serial_thread.start()
            self.btn_connect.setText("DISCONNECT"); self.btn_connect.setStyleSheet("background-color: #c62828; color: white; font-weight:bold; border-radius:4px;")
            self.combo_port.setEnabled(False); self.combo_baud.setEnabled(False)
    
    @Slot(list)
    def on_serial_data(self, data):
        if not self.is_test_mode:
            new_vals = [0.0] * 6
            for i in range(min(len(data), 6)): new_vals[i] = data[i]
            self.latest_serial_data = new_vals

    def handle_pop_out(self, channel_id):
        widget = self.plot_widgets[channel_id]; idx = self.grid_layout.indexOf(widget)
        row, col, rs, cs = self.grid_layout.getItemPosition(idx)
        self.grid_layout.removeWidget(widget); placeholder = PlaceholderWidget(channel_id)
        self.grid_layout.addWidget(placeholder, row, col, rs, cs); self.placeholders[channel_id] = placeholder
        dialog = DetachedWindow(channel_id, widget, self); dialog.window_closed.connect(self.handle_redock); dialog.show()
        widget.btn_pop.setText("↙"); widget.btn_pop.clicked.disconnect(); widget.btn_pop.clicked.connect(dialog.close)

    def handle_redock(self, channel_id):
        widget = self.plot_widgets[channel_id]
        if channel_id in self.placeholders:
            placeholder = self.placeholders[channel_id]; idx = self.grid_layout.indexOf(placeholder)
            row, col, rs, cs = self.grid_layout.getItemPosition(idx)
            self.grid_layout.removeWidget(placeholder); placeholder.deleteLater(); del self.placeholders[channel_id]
            self.grid_layout.addWidget(widget, row, col, rs, cs)
        if channel_id in self.detached_windows: del self.detached_windows[channel_id]
        widget.btn_pop.setText("↗"); widget.btn_pop.clicked.disconnect(); widget.btn_pop.clicked.connect(widget.request_pop_out)
    
    def on_timer_tick(self):
        if self.is_paused: return
        chunk_size = 30 
        self.data_buffer = np.roll(self.data_buffer, -chunk_size, axis=1); self.global_ptr += chunk_size
        t_start = self.global_ptr / self.fs; t_chunk = np.linspace(t_start, t_start + chunk_size/self.fs, chunk_size, dtype=np.float32)
        for i in range(6):
            ctrl = self.ctrl_widgets[i]; target = ctrl.spin.value()
            if ctrl.toggle.isChecked():
                if self.is_test_mode:
                    noise = np.random.normal(0, 2, chunk_size).astype(np.float32)
                    wave = 50 * np.sin(2 * np.pi * (1+i*0.2) * t_chunk)
                    new_data = target + wave + noise; current_val = new_data[-1]
                else:
                    val = self.latest_serial_data[i]; new_data = np.full(chunk_size, val, dtype=np.float32); current_val = val
            else: new_data = np.zeros(chunk_size, dtype=np.float32); current_val = 0.0
            self.data_buffer[i, -chunk_size:] = new_data
            ctrl.val_lbl.setText(f"{current_val:>9.2f}")
            self.plot_widgets[i+1].update_data(self.t_axis, self.data_buffer[i], target)

if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        win = MainWindow()
        win.show()
        sys.exit(app.exec())
    except Exception:
        traceback.print_exc()