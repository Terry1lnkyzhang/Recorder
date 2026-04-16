from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk


class ZoomableImageView(ttk.Frame):
    def __init__(self, master: tk.Misc, empty_text: str = "暂无预览") -> None:
        super().__init__(master)
        self.empty_text = empty_text
        self.original_image: Image.Image | None = None
        self.display_image: ImageTk.PhotoImage | None = None
        self.zoom = 1.0
        self.fit_zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._dragging = False
        self._drag_last_x = 0
        self._drag_last_y = 0
        self._image_item: int | None = None
        self._zoom_text_item: int | None = None
        self._status_text_item: int | None = None
        self._preview_after_id: str | None = None
        self._final_after_id: str | None = None
        self._fullscreen_window: tk.Toplevel | None = None
        self._fullscreen_view: ZoomableImageView | None = None

        self.canvas = tk.Canvas(self, bg="#111111", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)

        self.fullscreen_button = tk.Button(
            self,
            text="□",
            font=("Segoe UI", 10, "bold"),
            bd=0,
            relief=tk.FLAT,
            bg="#1f1f1f",
            fg="#ffffff",
            activebackground="#3a3a3a",
            activeforeground="#ffffff",
            cursor="hand2",
            command=self.open_fullscreen,
            padx=6,
            pady=1,
        )
        self.fullscreen_button.place(relx=1.0, x=-8, y=8, anchor="ne")
        self._sync_fullscreen_button_state()

    def set_image(self, image: Image.Image | None) -> None:
        self.original_image = image.copy() if image else None
        self.zoom = 1.0
        self.fit_zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._cancel_scheduled_renders()
        self._render(fit=True, resample=Image.Resampling.BILINEAR)
        self._schedule_finalize_render(fit=True)
        self._sync_fullscreen_button_state()
        self._refresh_fullscreen_view()

    def clear(self, text: str | None = None) -> None:
        self._cancel_scheduled_renders()
        self.original_image = None
        self.display_image = None
        self._ensure_canvas_items()
        self.canvas.itemconfigure(self._image_item, image="", state="hidden")
        self.canvas.itemconfigure(self._zoom_text_item, text="", state="hidden")
        self.canvas.coords(
            self._status_text_item,
            max(20, self.canvas.winfo_width() // 2),
            max(20, self.canvas.winfo_height() // 2),
        )
        self.canvas.itemconfigure(
            self._status_text_item,
            text=text or self.empty_text,
            fill="#d0d0d0",
            state="normal",
        )
        self._sync_fullscreen_button_state()
        self._refresh_fullscreen_view()

    def set_status(self, text: str) -> None:
        self._ensure_canvas_items()
        self.canvas.coords(
            self._status_text_item,
            max(20, self.canvas.winfo_width() // 2),
            max(20, self.canvas.winfo_height() // 2),
        )
        self.canvas.itemconfigure(
            self._status_text_item,
            text=text,
            fill="#d0d0d0",
            state="normal",
        )

    def open_fullscreen(self) -> None:
        if self.original_image is None:
            return
        if self._fullscreen_window is not None and self._fullscreen_window.winfo_exists():
            self._fullscreen_window.deiconify()
            self._fullscreen_window.lift()
            self._fullscreen_window.focus_force()
            self._refresh_fullscreen_view()
            return

        window = tk.Toplevel(self)
        window.title("全屏预览")
        window.configure(bg="#111111")
        window.attributes("-fullscreen", True)
        window.bind("<Escape>", lambda _event: self._close_fullscreen())
        window.protocol("WM_DELETE_WINDOW", self._close_fullscreen)

        toolbar = ttk.Frame(window)
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="关闭全屏", command=self._close_fullscreen).pack(side=tk.RIGHT, padx=12, pady=8)

        view = ZoomableImageView(window, empty_text=self.empty_text)
        view.pack(fill=tk.BOTH, expand=True)
        self._fullscreen_window = window
        self._fullscreen_view = view
        self._refresh_fullscreen_view()

    def _close_fullscreen(self) -> None:
        if self._fullscreen_window is not None and self._fullscreen_window.winfo_exists():
            self._fullscreen_window.destroy()
        self._fullscreen_window = None
        self._fullscreen_view = None

    def _refresh_fullscreen_view(self) -> None:
        if self._fullscreen_view is None:
            return
        if self.original_image is None:
            self._fullscreen_view.clear(self.empty_text)
        else:
            self._fullscreen_view.set_image(self.original_image)

    def _sync_fullscreen_button_state(self) -> None:
        self.fullscreen_button.configure(state=tk.NORMAL if self.original_image is not None else tk.DISABLED)

    def _on_configure(self, _event: tk.Event) -> None:
        if not self.original_image:
            self.clear()
            return
        self._schedule_configure_render()

    def _on_mousewheel(self, event: tk.Event) -> None:
        if not self.original_image:
            return

        old_scale = self.fit_zoom * self.zoom
        if old_scale <= 0:
            old_scale = 1.0

        step = 1.1 if event.delta > 0 else 0.9
        self.zoom = min(12.0, max(0.2, self.zoom * step))
        new_scale = self.fit_zoom * self.zoom

        pointer_x = self.canvas.canvasx(event.x)
        pointer_y = self.canvas.canvasy(event.y)
        image_x = (pointer_x - self.offset_x) / old_scale
        image_y = (pointer_y - self.offset_y) / old_scale
        self.offset_x = pointer_x - image_x * new_scale
        self.offset_y = pointer_y - image_y * new_scale
        self._render(resample=Image.Resampling.BILINEAR)
        self._schedule_finalize_render()

    def _on_drag_start(self, event: tk.Event) -> None:
        self._dragging = True
        self._drag_last_x = event.x
        self._drag_last_y = event.y

    def _on_drag_move(self, event: tk.Event) -> None:
        if not self._dragging or not self.original_image:
            return
        self.offset_x += event.x - self._drag_last_x
        self.offset_y += event.y - self._drag_last_y
        self._drag_last_x = event.x
        self._drag_last_y = event.y
        self._render(resample=Image.Resampling.BILINEAR)
        self._schedule_finalize_render()

    def _on_drag_end(self, _event: tk.Event) -> None:
        self._dragging = False

    def _render(self, fit: bool = False, resample: Image.Resampling = Image.Resampling.LANCZOS) -> None:
        if not self.original_image:
            self.clear()
            return

        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        image_width, image_height = self.original_image.size
        if image_width <= 0 or image_height <= 0:
            self.clear()
            return

        new_fit_zoom = min(canvas_width / image_width, canvas_height / image_height)
        if fit or self.fit_zoom <= 0:
            self.fit_zoom = new_fit_zoom
            self.zoom = 1.0
            scaled_width = image_width * self.fit_zoom
            scaled_height = image_height * self.fit_zoom
            self.offset_x = (canvas_width - scaled_width) / 2
            self.offset_y = (canvas_height - scaled_height) / 2
        elif abs(new_fit_zoom - self.fit_zoom) > 1e-6:
            ratio = new_fit_zoom / self.fit_zoom
            self.fit_zoom = new_fit_zoom
            self.offset_x *= ratio
            self.offset_y *= ratio

        scale = self.fit_zoom * self.zoom
        scaled_width = max(1, int(image_width * scale))
        scaled_height = max(1, int(image_height * scale))
        resized = self.original_image.resize((scaled_width, scaled_height), resample)
        self.display_image = ImageTk.PhotoImage(resized)
        self._ensure_canvas_items()
        self.canvas.coords(self._image_item, self.offset_x, self.offset_y)
        self.canvas.itemconfigure(self._image_item, image=self.display_image, state="normal")
        self.canvas.coords(self._zoom_text_item, 12, 12)
        self.canvas.itemconfigure(
            self._zoom_text_item,
            text=f"{int(scale * 100)}%",
            fill="#ffffff",
            state="normal",
        )
        self.canvas.itemconfigure(self._status_text_item, text="", state="hidden")

    def _schedule_configure_render(self) -> None:
        self._cancel_after("preview")
        self._cancel_after("final")
        self._preview_after_id = self.after(24, lambda: self._run_scheduled_render(fit=True, resample=Image.Resampling.BILINEAR, mode="preview"))
        self._final_after_id = self.after(140, lambda: self._run_scheduled_render(fit=True, resample=Image.Resampling.LANCZOS, mode="final"))

    def _schedule_finalize_render(self, fit: bool = False) -> None:
        self._cancel_after("final")
        self._final_after_id = self.after(100, lambda: self._run_scheduled_render(fit=fit, resample=Image.Resampling.LANCZOS, mode="final"))

    def _run_scheduled_render(self, fit: bool, resample: Image.Resampling, mode: str) -> None:
        if mode == "preview":
            self._preview_after_id = None
        else:
            self._final_after_id = None
        if not self.original_image:
            return
        self._render(fit=fit, resample=resample)

    def _cancel_scheduled_renders(self) -> None:
        self._cancel_after("preview")
        self._cancel_after("final")

    def _cancel_after(self, mode: str) -> None:
        after_id = self._preview_after_id if mode == "preview" else self._final_after_id
        if not after_id:
            return
        try:
            self.after_cancel(after_id)
        except Exception:
            pass
        if mode == "preview":
            self._preview_after_id = None
        else:
            self._final_after_id = None

    def _ensure_canvas_items(self) -> None:
        if self._image_item is None:
            self._image_item = self.canvas.create_image(0, 0, anchor=tk.NW, state="hidden")
        if self._zoom_text_item is None:
            self._zoom_text_item = self.canvas.create_text(
                12,
                12,
                anchor=tk.NW,
                fill="#ffffff",
                font=("Segoe UI", 10, "bold"),
                state="hidden",
            )
        if self._status_text_item is None:
            self._status_text_item = self.canvas.create_text(
                max(20, self.canvas.winfo_width() // 2),
                max(20, self.canvas.winfo_height() // 2),
                text=self.empty_text,
                fill="#d0d0d0",
                font=("Segoe UI", 12),
                anchor=tk.CENTER,
                state="hidden",
            )