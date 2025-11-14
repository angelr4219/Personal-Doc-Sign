#!/usr/bin/env python3
"""
Mini DocuSign-like PDF editor (with drag-and-drop + saved signatures +
draggable & resizable signature box)

Features:
- Drag & drop a PDF onto the window to open it
- Or use File → Open PDF…
- Show first page
- "Signature Mode": click on the page to create a signature box
- Drag the box to move it
- Use "Bigger"/"Smaller" buttons to resize the box
- "New Signature": draw your signature with the mouse, saved to ./signatures/
- "Choose Saved Signature": pick any saved signature image
- "Save filled PDF": stamps the chosen signature image into the box
"""

import sys
import os
import time
from dataclasses import dataclass
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QAction, QFileDialog,
    QLabel, QVBoxLayout, QHBoxLayout, QWidget, QPushButton,
    QMessageBox, QDialog
)
from PyQt5.QtGui import (
    QPixmap, QImage, QPainter, QPen
)
from PyQt5.QtCore import Qt, QPoint, QRect

import fitz  # PyMuPDF


# ---------- Data model for signature placement ----------

@dataclass
class SignatureField:
    """Data model for a single signature field on a page."""
    page_index: int
    x: float  # PDF coordinates
    y: float
    width: float
    height: float


# ---------- Signature drawing canvas & dialog ----------

class SignatureCanvas(QWidget):
    """
    A simple white canvas where the user can draw with the mouse.
    We store the drawing in a QPixmap.
    """

    def __init__(self, width=500, height=200, parent=None):
        super().__init__(parent)
        self.setFixedSize(width, height)

        self.image = QPixmap(width, height)
        self.image.fill(Qt.white)

        self.drawing = False
        self.last_point: Optional[QPoint] = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self.image)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drawing = True
            self.last_point = event.pos()

    def mouseMoveEvent(self, event):
        if self.drawing and self.last_point is not None:
            painter = QPainter(self.image)
            pen = QPen(Qt.black, 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(self.last_point, event.pos())
            painter.end()
            self.last_point = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drawing = False

    def clear(self):
        self.image.fill(Qt.white)
        self.update()

    def get_pixmap(self) -> QPixmap:
        return self.image


class SignatureDialog(QDialog):
    """
    Dialog that contains a SignatureCanvas and buttons to Clear / Save / Cancel.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Signature")
        self.canvas = SignatureCanvas(parent=self)

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()
        layout.addWidget(QLabel("Draw your signature below:", self))
        layout.addWidget(self.canvas)

        button_row = QHBoxLayout()
        self.btn_clear = QPushButton("Clear")
        self.btn_save = QPushButton("Save")
        self.btn_cancel = QPushButton("Cancel")

        self.btn_clear.clicked.connect(self.canvas.clear)
        self.btn_save.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

        button_row.addWidget(self.btn_clear)
        button_row.addStretch(1)
        button_row.addWidget(self.btn_cancel)
        button_row.addWidget(self.btn_save)

        layout.addLayout(button_row)
        self.setLayout(layout)

    def get_pixmap(self) -> QPixmap:
        return self.canvas.get_pixmap()


# ---------- PDF page display widget ----------

class PdfPageLabel(QLabel):
    """
    QLabel that displays a rendered PDF page and lets user
    click to place a signature field and drag/resize the box.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self.parent_window: "MainWindow" = parent  # type: ignore
        self.signature_mode = False

        self.preview_rect: Optional[QRect] = None
        self.pdf_doc: Optional[fitz.Document] = None
        self.page_index: int = 0
        self.page_pixmap: Optional[QPixmap] = None
        self.page_scale: float = 1.0  # pixel per PDF point

        # For dragging the box
        self.dragging = False
        self.drag_offset = QPoint(0, 0)

        # Default box size in pixels
        self.box_width_px = 150
        self.box_height_px = 50

    def load_pdf_page(self, doc: fitz.Document, page_index: int = 0, zoom: float = 1.5):
        self.pdf_doc = doc
        self.page_index = page_index

        page = doc[page_index]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        # Convert pixmap to QImage
        img = QImage(
            pix.samples, pix.width, pix.height, pix.stride,
            QImage.Format_RGB888
        )
        self.page_pixmap = QPixmap.fromImage(img)
        self.page_scale = zoom  # 1 PDF point * zoom = pixels
        self.setPixmap(self.page_pixmap)
        self.setFixedSize(self.page_pixmap.size())

        # Reset box when loading new page
        self.preview_rect = None
        self.dragging = False
        self.update()

    def set_signature_mode(self, enabled: bool):
        self.signature_mode = enabled
        # Don’t clear the box on each toggle; user may want to move it later
        self.update()

    def resize_box(self, factor: float):
        """
        Resize the box width/height around its center.
        factor > 1 → bigger, 0 < factor < 1 → smaller.
        """
        if self.preview_rect is None:
            # If no box yet, just change default size; next click will use it
            self.box_width_px = max(20, int(self.box_width_px * factor))
            self.box_height_px = max(10, int(self.box_height_px * factor))
            return

        rect = self.preview_rect
        center = rect.center()
        new_w = max(20, int(rect.width() * factor))
        new_h = max(10, int(rect.height() * factor))

        # Create new rect centered at old center
        new_rect = QRect(
            center.x() - new_w // 2,
            center.y() - new_h // 2,
            new_w,
            new_h,
        )
        self.preview_rect = self._clamp_rect_to_pixmap(new_rect)
        self.update()

        # Inform parent about updated coordinates
        self._update_signature_field_from_rect()

    def _clamp_rect_to_pixmap(self, rect: QRect) -> QRect:
        """Keep the rectangle fully inside the pixmap boundaries."""
        if self.page_pixmap is None:
            return rect

        max_x = self.page_pixmap.width() - rect.width()
        max_y = self.page_pixmap.height() - rect.height()

        x = max(0, min(rect.x(), max_x))
        y = max(0, min(rect.y(), max_y))

        return QRect(x, y, rect.width(), rect.height())

    def mousePressEvent(self, event):
        if self.page_pixmap is None or self.pdf_doc is None:
            return

        if event.button() == Qt.LeftButton:
            click_pos = event.pos()

            # If we already have a box and the click is inside it → start dragging
            if self.preview_rect is not None and self.preview_rect.contains(click_pos):
                self.dragging = True
                self.drag_offset = click_pos - self.preview_rect.topLeft()
                return

            # Otherwise, only create a new box if signature mode is on
            if self.signature_mode:
                rect = QRect(
                    click_pos.x(),
                    click_pos.y(),
                    self.box_width_px,
                    self.box_height_px,
                )
                self.preview_rect = self._clamp_rect_to_pixmap(rect)
                self.update()
                self._update_signature_field_from_rect()

    def mouseMoveEvent(self, event):
        if not self.dragging or self.preview_rect is None or self.page_pixmap is None:
            return

        pos = event.pos()
        # New top-left = mouse position minus initial offset
        new_top_left = pos - self.drag_offset
        new_rect = QRect(new_top_left, self.preview_rect.size())
        self.preview_rect = self._clamp_rect_to_pixmap(new_rect)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            self._update_signature_field_from_rect()

    def _update_signature_field_from_rect(self):
        """Convert the current preview_rect into a SignatureField and send to parent."""
        if self.preview_rect is None or self.page_pixmap is None or self.pdf_doc is None:
            return

        rect = self.preview_rect

        # Convert widget/pixel coordinates to PDF coordinates
        pdf_x = rect.x() / self.page_scale
        pdf_y = rect.y() / self.page_scale
        pdf_w = rect.width() / self.page_scale
        pdf_h = rect.height() / self.page_scale

        field = SignatureField(
            page_index=self.page_index,
            x=pdf_x,
            y=pdf_y,
            width=pdf_w,
            height=pdf_h,
        )
        self.parent_window.set_signature_field(field)

    def paintEvent(self, event):
        # First draw the PDF page (pixmap)
        super().paintEvent(event)

        # Then draw the red dashed box on top
        if self.preview_rect is not None:
            painter = QPainter(self)
            pen = QPen(Qt.red, 2, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(self.preview_rect)
            painter.end()


# ---------- Main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Mini DocuSign (Python Demo)")
        # Enable drag & drop on the main window
        self.setAcceptDrops(True)

        self.pdf_doc: Optional[fitz.Document] = None
        self.signature_field: Optional[SignatureField] = None
        self.current_signature_path: Optional[str] = None  # selected/created signature image

        self._build_ui()
        self._build_menu()

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout()

        self.pdf_label = PdfPageLabel(parent=self)

        # Buttons row
        buttons_row = QHBoxLayout()

        self.sig_button = QPushButton("Signature Mode (Click on page)")
        self.sig_button.setCheckable(True)
        self.sig_button.clicked.connect(self.on_signature_mode_toggled)

        self.new_sig_button = QPushButton("New Signature")
        self.new_sig_button.clicked.connect(self.create_new_signature)

        self.old_sig_button = QPushButton("Choose Saved Signature")
        self.old_sig_button.clicked.connect(self.choose_saved_signature)

        self.bigger_button = QPushButton("Bigger Box")
        self.bigger_button.clicked.connect(lambda: self.pdf_label.resize_box(1.2))

        self.smaller_button = QPushButton("Smaller Box")
        self.smaller_button.clicked.connect(lambda: self.pdf_label.resize_box(0.8))

        buttons_row.addWidget(self.sig_button)
        buttons_row.addStretch(1)
        buttons_row.addWidget(self.bigger_button)
        buttons_row.addWidget(self.smaller_button)
        buttons_row.addStretch(1)
        buttons_row.addWidget(self.new_sig_button)
        buttons_row.addWidget(self.old_sig_button)

        layout.addWidget(self.pdf_label, alignment=Qt.AlignCenter)
        layout.addLayout(buttons_row)

        central.setLayout(layout)
        self.setCentralWidget(central)

        self.statusBar().showMessage("Open or drag & drop a PDF to begin")

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")

        open_act = QAction("Open PDF…", self)
        open_act.triggered.connect(self.open_pdf_dialog)
        file_menu.addAction(open_act)

        save_act = QAction("Save filled PDF as…", self)
        save_act.triggered.connect(self.save_filled_pdf)
        file_menu.addAction(save_act)

        file_menu.addSeparator()

        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

    # ---------- Drag & Drop handlers ----------

    def dragEnterEvent(self, event):
        """Allow drag if at least one of the URLs is a local PDF file."""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        """Open the first dropped PDF file."""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                    path = url.toLocalFile()
                    self.open_pdf_path(path)
                    event.acceptProposedAction()
                    return
        event.ignore()

    # ---------- PDF opening helpers ----------

    def open_pdf_dialog(self):
        """Open PDF via file dialog."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF files (*.pdf)"
        )
        if not path:
            return
        self.open_pdf_path(path)

    def open_pdf_path(self, path: str):
        """Open PDF from a given file path (used by dialog & drag-and-drop)."""
        try:
            doc = fitz.open(path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open PDF:\n{e}")
            return

        self.pdf_doc = doc
        self.signature_field = None
        self.pdf_label.preview_rect = None

        # For now just load page 0
        self.pdf_label.load_pdf_page(doc, page_index=0, zoom=1.5)
        self.statusBar().showMessage(f"Loaded: {path}")

    # ---------- Signature mode & field placement ----------

    def on_signature_mode_toggled(self, checked: bool):
        if self.pdf_doc is None:
            QMessageBox.information(self, "Info", "Open or drop a PDF first.")
            self.sig_button.setChecked(False)
            return

        self.pdf_label.set_signature_mode(checked)
        if checked:
            self.statusBar().showMessage("Signature mode: click once to create the box; drag to move it.")
        else:
            self.statusBar().showMessage("Signature mode off. You can still drag the box.")

    def set_signature_field(self, field: SignatureField):
        self.signature_field = field
        self.statusBar().showMessage(
            f"Signature box at page {field.page_index}, x={field.x:.1f}, y={field.y:.1f}, "
            f"w={field.width:.1f}, h={field.height:.1f}"
        )

    # ---------- Signature creation & selection ----------

    def create_new_signature(self):
        """Open the drawing dialog, save signature to disk, and select it."""
        dlg = SignatureDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            pixmap = dlg.get_pixmap()

            # Ensure signatures directory exists
            sig_dir = os.path.join(os.getcwd(), "signatures")
            os.makedirs(sig_dir, exist_ok=True)

            # Auto-generate filename with timestamp
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"signature_{ts}.png"
            path = os.path.join(sig_dir, filename)

            # Save the drawn signature as PNG
            if not pixmap.save(path, "PNG"):
                QMessageBox.critical(self, "Error", "Failed to save signature image.")
                return

            self.current_signature_path = path
            self.statusBar().showMessage(f"New signature saved and selected: {path}")
        else:
            self.statusBar().showMessage("New signature canceled.")

    def choose_saved_signature(self):
        """Let the user pick a previously saved signature image."""
        sig_dir = os.path.join(os.getcwd(), "signatures")
        if not os.path.isdir(sig_dir):
            sig_dir = os.getcwd()

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose saved signature",
            sig_dir,
            "Images (*.png *.jpg *.jpeg)"
        )
        if not path:
            return

        self.current_signature_path = path
        self.statusBar().showMessage(f"Selected signature: {path}")

    # ---------- Saving filled PDF ----------

    def save_filled_pdf(self):
        if self.pdf_doc is None:
            QMessageBox.information(self, "Info", "Open or drop a PDF first.")
            return
        if self.signature_field is None:
            QMessageBox.information(self, "Info", "Place and adjust the signature box first.")
            return
        if self.current_signature_path is None:
            QMessageBox.information(self, "Info", "Create or choose a signature first.")
            return

        # Ask for output path
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save filled PDF as…", "", "PDF files (*.pdf)"
        )
        if not out_path:
            return

        try:
            # Work on the current document
            doc = self.pdf_doc
            page = doc[self.signature_field.page_index]

            field = self.signature_field

            # PDF coordinate system in fitz has origin at top-left by default.
            rect = fitz.Rect(
                field.x,
                field.y,
                field.x + field.width,
                field.y + field.height,
            )

            # Insert signature image into the rect
            page.insert_image(rect, filename=self.current_signature_path, keep_proportion=True)

            # Save out
            doc.save(out_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save PDF:\n{e}")
            return

        QMessageBox.information(self, "Saved", f"Filled PDF saved to:\n{out_path}")
        self.statusBar().showMessage(f"Filled PDF saved: {out_path}")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(900, 900)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
