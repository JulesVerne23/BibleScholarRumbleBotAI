# =============================================================================
# BibleScholarRumble64.py
# BibleScholar — Main UI
#
# This file is UI only.  All AI logic lives in scholar_engine.py.
# All Rumble bot logic lives in rumble_engine.py.
#
# Resources (NET Bible, Strong's, Heiser) load in background threads after
# the window appears — a status bar shows progress.
# =============================================================================

import os
import tkinter as tk
from tkinter import scrolledtext, messagebox, simpledialog
from queue import Queue, Empty
import threading

# ── Engine imports ────────────────────────────────────────────────────────────
import scholar_engine as se
import rumble_engine  as re_engine

# ── Optional dotenv ───────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# RUMBLE CREDENTIALS & BOT SETTINGS  (read from env / .env)
# =============================================================================
RUMBLE_USERNAME = os.getenv("RUMBLE_USERNAME", "")
RUMBLE_PASSWORD = os.getenv("RUMBLE_PASSWORD", "")
BOT_NAME        = os.getenv("RUMBLE_BOT_NAME", "BibleScholar23")

# =============================================================================
# ROOT WINDOW
# =============================================================================
root = tk.Tk()
root.title("BibleScholar — Rumble Edition")
root.geometry("1200x900")
root.configure(bg="#101015")

# ── Ollama health check ───────────────────────────────────────────────────────
if not se.check_ollama_health():
    messagebox.showerror(
        "Ollama Not Found",
        "Could not connect to Ollama at http://localhost:11434.\n\n"
        "Please ensure Ollama is running before using BibleScholar.\n"
        "Start it with:  ollama serve"
    )

# =============================================================================
# THEME
# =============================================================================
accent_color = "#00b7ff"
accent_soft  = "#0078a0"
panel_bg     = "#15151c"
chat_bg      = "#1b1b23"
text_fg      = "#d4d4d4"
border_color = "#303038"

root.grid_columnconfigure(1, weight=1)
root.grid_columnconfigure(2, weight=0)
root.grid_rowconfigure(0, weight=3)
root.grid_rowconfigure(1, weight=1)
root.grid_rowconfigure(2, weight=1)

# =============================================================================
# AI STOP FLAG
# =============================================================================
_ai_stop_flag = {"stop": False}

def stop_ai_response():
    _ai_stop_flag["stop"] = True

# =============================================================================
# SIDEBAR — saved conversations
# =============================================================================
sidebar = tk.Frame(root, bg=panel_bg)
sidebar.grid(row=0, column=0, sticky="nsw")
sidebar.grid_rowconfigure(2, weight=1)

tk.Label(sidebar, text="Conversations", bg=panel_bg, fg=accent_color,
         font=("Segoe UI", 11, "bold")).pack(padx=10, pady=(10, 5), anchor="w")

convo_listbox = tk.Listbox(
    sidebar, bg="#121218", fg=text_fg,
    selectbackground=accent_soft, selectforeground="white",
    borderwidth=0, highlightthickness=0, font=("Segoe UI", 10)
)
convo_listbox.pack(padx=10, pady=(0, 10), fill=tk.BOTH, expand=True)

sidebar_buttons_frame = tk.Frame(sidebar, bg=panel_bg)
sidebar_buttons_frame.pack(padx=10, pady=(0, 10), fill=tk.X)

# =============================================================================
# MAIN CHAT FRAME
# =============================================================================
main_frame = tk.Frame(root, bg="#101015")
main_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 5), pady=10)
main_frame.grid_rowconfigure(0, weight=1)
main_frame.grid_columnconfigure(0, weight=1)

chat_window = scrolledtext.ScrolledText(
    main_frame, wrap=tk.WORD, state="disabled",
    font=("Consolas", 12), bg=chat_bg, fg=text_fg,
    insertbackground=text_fg, borderwidth=0, relief="flat",
    highlightthickness=1, highlightbackground=border_color
)
chat_window.grid(row=0, column=0, columnspan=4, sticky="nsew", pady=(0, 8))
chat_window.tag_config("user",    foreground=accent_color,  font=("Consolas", 12, "bold"))
chat_window.tag_config("bot",     foreground="#39FF14")
chat_window.tag_config("verse",   foreground="#1E90FF",     font=("Consolas", 12, "bold"))
chat_window.tag_config("strongs", foreground="#1E90FF",     font=("Consolas", 12, "bold"))

thinking_label = tk.Label(
    main_frame, text="", bg="#101015", fg=accent_color,
    font=("Segoe UI", 9, "italic")
)
thinking_label.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 3))

# =============================================================================
# INPUT BAR
# =============================================================================
entry = tk.Entry(
    main_frame, font=("Consolas", 12),
    bg="#1a1a22", fg="#ffffff",
    insertbackground="#00b7ff",
    relief="flat",
    highlightthickness=2,
    highlightbackground="#303040",
    highlightcolor=accent_color
)
entry.grid(row=2, column=0, sticky="ew", padx=(0, 5), ipady=6)

send_button = tk.Button(
    main_frame, text="Send", command=lambda: send_message(),
    font=("Segoe UI", 11, "bold"),
    bg=accent_color, fg="black",
    activebackground="#33c7ff",
    relief="flat", padx=14, pady=6
)
send_button.grid(row=2, column=1, sticky="ew")

ai_stop_button = tk.Button(
    main_frame, text="⏹ Stop AI", command=stop_ai_response,
    font=("Segoe UI", 10, "bold"),
    bg="#7a2020", fg="white",
    activebackground="#a03030",
    relief="flat", padx=10, pady=6,
    state="disabled"
)
ai_stop_button.grid(row=2, column=2, sticky="ew", padx=(5, 0))

copy_button = tk.Button(
    main_frame, text="Copy Verses", command=lambda: copy_verses_to_clipboard(),
    font=("Segoe UI", 9),
    bg="#2a2a33", fg="white",
    activebackground="#404050",
    relief="flat", padx=10, pady=5
)
copy_button.grid(row=2, column=3, sticky="ew", padx=(5, 0))

# =============================================================================
# VERSE LOOKUP PANEL
# =============================================================================
verse_frame = tk.Frame(main_frame, bg="#101015")
verse_frame.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
verse_frame.grid_columnconfigure(1, weight=1)

tk.Label(verse_frame, text="Verse lookup:", bg="#101015", fg=text_fg,
         font=("Segoe UI", 9)).grid(row=0, column=0, padx=(0, 5))

verse_entry = tk.Entry(
    verse_frame, font=("Consolas", 11),
    bg="#202028", fg="#ffffff", insertbackground="#ffffff",
    borderwidth=2, relief="flat"
)
verse_entry.grid(row=0, column=1, sticky="ew", padx=(0, 5))

tk.Button(
    verse_frame, text="Go", command=lambda: lookup_verse(),
    font=("Segoe UI", 9), bg=accent_soft, fg="white",
    activebackground=accent_color, relief="flat", padx=8, pady=3
).grid(row=0, column=2)

# =============================================================================
# NET BIBLE PANEL (bottom strip)
# =============================================================================
kjv_frame = tk.Frame(root, bg="#181820", height=160)
kjv_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
kjv_frame.grid_propagate(False)
kjv_frame.grid_columnconfigure(0, weight=1)

tk.Frame(kjv_frame, bg="#303038", height=2).grid(row=0, column=0, sticky="ew")
tk.Label(kjv_frame, text="NET Bible Verses", bg="#181820", fg=accent_color,
         font=("Segoe UI", 11, "bold")).grid(row=1, column=0, sticky="w", padx=10, pady=(5, 0))

verse_output = scrolledtext.ScrolledText(
    kjv_frame, height=6, wrap=tk.WORD,
    font=("Consolas", 11), bg="#121218", fg="#d4d4d4",
    insertbackground="#d4d4d4", borderwidth=0, relief="flat"
)
verse_output.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
verse_output.config(state="disabled")

# =============================================================================
# STRONG'S PANEL (right column)
# =============================================================================
strongs_panel = tk.Frame(root, bg=panel_bg, width=220)
strongs_panel.grid(row=0, column=2, sticky="nsew", padx=(0, 10), pady=10)
strongs_panel.grid_propagate(False)
strongs_panel.grid_rowconfigure(1, weight=1)
strongs_panel.grid_columnconfigure(0, weight=1)

tk.Label(strongs_panel, text="Strong's Words", bg=panel_bg, fg="#ffcc44",
         font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))

strongs_output = scrolledtext.ScrolledText(
    strongs_panel, wrap=tk.WORD,
    font=("Consolas", 10), bg="#121218", fg="#d4d4d4",
    insertbackground="#d4d4d4", borderwidth=0, relief="flat"
)
strongs_output.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 10))
strongs_output.tag_config("strongs_num",     foreground="#ffcc44", font=("Consolas", 10, "bold"))
strongs_output.tag_config("strongs_lang",    foreground="#888888", font=("Consolas", 10, "italic"))
strongs_output.tag_config("strongs_word",    foreground="#d4d4d4", font=("Consolas", 10))
strongs_output.tag_config("strongs_missing", foreground="#ff6666", font=("Consolas", 10, "italic"))
strongs_output.config(state="disabled")

# =============================================================================
# LOADING STATUS BAR  — shown during background resource loading
# =============================================================================
status_bar = tk.Label(
    root, text="⏳ Loading resources…", bg="#0d0d12", fg="#888888",
    font=("Segoe UI", 8), anchor="w"
)
status_bar.grid(row=3, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 2))

_status_items: dict[str, bool] = {
    "NET Bible": False,
    "Strong's":  False,
    "Heiser KB": False,
}

def _on_resource_ready(label: str):
    """Called from background threads via root.after — updates the status bar."""
    _status_items[label] = True
    ready   = [k for k, v in _status_items.items() if v]
    pending = [k for k, v in _status_items.items() if not v]
    if pending:
        status_bar.config(
            text=f"✓ {', '.join(ready)} ready  |  ⏳ Loading: {', '.join(pending)}…",
            fg="#888888"
        )
    else:
        status_bar.config(text="✓ All resources loaded — ready.", fg="#00ff88")

def _status_callback(msg: str):
    """Passed to start_background_loading; schedules UI update on main thread."""
    label = msg.replace("✓ ", "").replace(" ready", "").strip()
    root.after(0, lambda: _on_resource_ready(label))

# =============================================================================
# DISPLAY HELPERS
# =============================================================================
def highlight_verses(text_widget, start_index="1.0"):
    text_widget.tag_remove("verse", start_index, tk.END)
    text = text_widget.get(start_index, tk.END)
    for match in se.VERSE_PATTERN.finditer(text):
        s = f"{start_index}+{match.start()}c"
        e = f"{start_index}+{match.end()}c"
        text_widget.tag_add("verse", s, e)

def highlight_strongs(text_widget, start_index="1.0"):
    text_widget.tag_remove("strongs", start_index, tk.END)
    text = text_widget.get(start_index, tk.END)
    for match in se.STRONGS_PATTERN.finditer(text):
        raw = match.group(1) or match.group(2) or match.group(3)
        if not raw:
            continue
        num_start = text.find(raw, match.start())
        if num_start == -1:
            continue
        num_end  = num_start + len(raw)
        tag_s    = f"{start_index}+{num_start}c"
        tag_e    = f"{start_index}+{num_end}c"
        text_widget.tag_add("strongs", tag_s, tag_e)

def _highlight_new_content(text_widget):
    try:
        ranges = text_widget.tag_ranges("highlight_marker")
        start  = str(ranges[-1]) if ranges else "1.0"
    except Exception:
        start = "1.0"
    highlight_verses(text_widget,  start_index=start)
    highlight_strongs(text_widget, start_index=start)
    text_widget.tag_remove("highlight_marker", "1.0", tk.END)
    text_widget.tag_add("highlight_marker", tk.END)

def display_net_verses(refs):
    verse_output.config(state="normal")
    verse_output.delete("1.0", tk.END)
    if not refs:
        verse_output.insert(tk.END, "No verse references detected.\n")
        verse_output.config(state="disabled")
        return
    for ref in refs:
        parsed  = se.parse_reference(ref)
        if not parsed:
            verse_output.insert(tk.END, f"{ref} — [Could not parse reference]\n\n")
            continue
        book    = se.BOOK_NORMALIZATION.get(parsed["book"], parsed["book"])
        chapter = str(parsed["chapter"])
        for verse in parsed["verses"]:
            text = se.NET_LOOKUP.get(book, {}).get(chapter, {}).get(str(verse))
            if text:
                verse_output.insert(tk.END, f"{book} {chapter}:{verse} — {text}\n\n")
            else:
                verse_output.insert(tk.END, f"{book} {chapter}:{verse} — [Not found in NET Bible]\n\n")
    verse_output.config(state="disabled")

def display_strongs_words(strongs_numbers):
    strongs_output.config(state="normal")
    strongs_output.delete("1.0", tk.END)
    if not strongs_numbers:
        strongs_output.insert(tk.END, "No Strong's numbers detected.\n")
        strongs_output.config(state="disabled")
        return
    for num in strongs_numbers:
        word = se.STRONGS_LOOKUP.get(num)
        lang = "Heb." if num.startswith("H") else "Grk."
        if word:
            strongs_output.insert(tk.END, f"{num}  ", "strongs_num")
            strongs_output.insert(tk.END, f"({lang})  ", "strongs_lang")
            strongs_output.insert(tk.END, f"{word}\n\n", "strongs_word")
        else:
            strongs_output.insert(tk.END, f"{num}  ", "strongs_num")
            strongs_output.insert(tk.END, "[Not found in Strong's]\n\n", "strongs_missing")
    strongs_output.config(state="disabled")

def copy_verses_to_clipboard():
    ranges = chat_window.tag_ranges("verse")
    if not ranges:
        messagebox.showinfo("Copy Verses", "No verse references detected.")
        return
    verses = [chat_window.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)]
    root.clipboard_clear()
    root.clipboard_append("\n".join(sorted(set(verses))))
    messagebox.showinfo("Copy Verses", "Verse references copied.")

def _clear_widget(widget):
    widget.config(state="normal")
    widget.delete("1.0", tk.END)
    widget.config(state="disabled")

# =============================================================================
# STREAMING CHAT (Scholar UI)
# =============================================================================
def _stream_to_chat(prompt: str, on_done=None):
    _ai_stop_flag["stop"] = False

    chat_window.config(state="normal")
    chat_window.tag_add("highlight_marker", tk.END)
    chat_window.insert(tk.END, "BibleScholar:\n ", "bot")
    chat_window.config(state="disabled")
    chat_window.see(tk.END)

    send_button.config(state="disabled")
    ai_stop_button.config(state="normal")
    thinking_label.config(text="BibleScholar is thinking…")
    root.update_idletasks()

    q          = Queue()
    ai_chunks  = []

    def worker():
        for chunk in se.stream_from_ollama(prompt):
            if _ai_stop_flag["stop"]:
                break
            q.put(chunk)
        q.put(None)

    def _finish(stopped=False):
        thinking_label.config(text="[Stopped]" if stopped else "")
        chat_window.config(state="normal")
        chat_window.insert(tk.END, "\n\n")
        _highlight_new_content(chat_window)
        chat_window.config(state="disabled")
        send_button.config(state="normal")
        ai_stop_button.config(state="disabled")
        ai_full = "".join(ai_chunks)
        if on_done and not stopped:
            on_done(ai_full)

    def process_queue():
        if _ai_stop_flag["stop"]:
            try:
                while True:
                    q.get_nowait()
            except Empty:
                pass
            _finish(stopped=True)
            return
        try:
            while True:
                item = q.get_nowait()
                if item is None:
                    _finish(stopped=False)
                    return
                ai_chunks.append(item)
                chat_window.config(state="normal")
                chat_window.insert(tk.END, item, "bot")
                chat_window.config(state="disabled")
                chat_window.see(tk.END)
        except Empty:
            pass
        root.after(30, process_queue)

    threading.Thread(target=worker, daemon=True).start()
    root.after(30, process_queue)

# =============================================================================
# CHAT LOGIC
# =============================================================================
conversations = se.load_saved_conversations()

def send_message():
    user_input = entry.get()
    if not user_input.strip():
        return
    chat_window.config(state="normal")
    chat_window.insert(tk.END, f"\nYou: {user_input}\n", "user")
    chat_window.config(state="disabled")
    entry.delete(0, tk.END)

    def on_done(ai_full):
        refs = se.extract_references(ai_full)
        display_net_verses(refs)
        display_strongs_words(se.extract_strongs_numbers(ai_full))

    _stream_to_chat(user_input, on_done=on_done)

def save_conversation():
    content = chat_window.get("1.0", tk.END).strip()
    if not content:
        messagebox.showinfo("Save Conversation", "Nothing to save.")
        return
    title = simpledialog.askstring("Save Conversation", "Enter a title:")
    if not title:
        return
    conversations.append((title, content))
    convo_listbox.insert(tk.END, title)
    se.persist_conversations(conversations)

def delete_conversation():
    sel = convo_listbox.curselection()
    if not sel:
        return
    idx   = sel[0]
    title = conversations[idx][0]
    if not messagebox.askyesno("Delete", f"Delete '{title}'?"):
        return
    conversations.pop(idx)
    convo_listbox.delete(idx)
    se.persist_conversations(conversations)

def load_conversation(event=None):
    sel = convo_listbox.curselection()
    if not sel:
        return
    _, content = conversations[sel[0]]
    chat_window.config(state="normal")
    chat_window.delete("1.0", tk.END)
    chat_window.insert(tk.END, content)
    highlight_verses(chat_window, "1.0")
    highlight_strongs(chat_window, "1.0")
    chat_window.tag_remove("highlight_marker", "1.0", tk.END)
    chat_window.tag_add("highlight_marker", tk.END)
    chat_window.config(state="disabled")

def new_conversation():
    se.conversation_history.clear()
    _clear_widget(chat_window)
    _clear_widget(verse_output)
    _clear_widget(strongs_output)

def lookup_verse():
    ref = verse_entry.get().strip()
    if not ref:
        return
    prompt = (
        f"Quote the NET Bible (New English Translation) text of {ref} "
        "word for word, exactly as written. "
        "Give only the reference and the verse text — nothing else. "
        "No commentary, no explanation, no paraphrasing. "
        "End the reference with (NET)."
    )
    chat_window.config(state="normal")
    chat_window.insert(tk.END, f"Verse lookup ({ref}):\n", "user")
    chat_window.config(state="disabled")
    chat_window.see(tk.END)
    verse_entry.delete(0, tk.END)

    def on_done(ai_full):
        display_strongs_words(se.extract_strongs_numbers(ai_full))

    _stream_to_chat(prompt, on_done=on_done)

convo_listbox.bind("<<ListboxSelect>>", load_conversation)

# Sidebar buttons
for label, cmd in [("Save", save_conversation), ("New", new_conversation), ("Delete", delete_conversation)]:
    btn_cfg = {"Save":   {"bg": accent_soft,  "fg": "white", "activebackground": accent_color},
               "New":    {"bg": "#303040",    "fg": "white", "activebackground": "#404050"},
               "Delete": {"bg": "#5a2020",    "fg": "white", "activebackground": "#7a3030"}}[label]
    tk.Button(sidebar_buttons_frame, text=label, command=cmd,
              font=("Segoe UI", 9), relief="flat", padx=6, pady=3,
              **btn_cfg).pack(side=tk.LEFT, padx=(0, 5))

for title, _ in conversations:
    convo_listbox.insert(tk.END, title)

# =============================================================================
# RUMBLE BOT PANEL
# =============================================================================
rumble_outer = tk.Frame(root, bg="#0d1a0d", bd=0)
rumble_outer.grid(row=2, column=0, columnspan=3, sticky="nsew")
rumble_outer.grid_columnconfigure(0, weight=1)
rumble_outer.grid_rowconfigure(1, weight=1)

# ── drag handle ───────────────────────────────────────────────────────────────
_drag_start_y = [0]
_drag_start_h = [0]
drag_handle   = tk.Frame(rumble_outer, bg="#1a3a1a", height=6, cursor="sb_v_double_arrow")
drag_handle.grid(row=0, column=0, sticky="ew")
drag_handle.grid_propagate(False)
tk.Label(drag_handle, text="⠿  drag to resize", bg="#1a3a1a", fg="#336633",
         font=("Segoe UI", 7)).pack(side=tk.LEFT, padx=8)

def _drag_start(event):
    _drag_start_y[0] = event.y_root
    _drag_start_h[0] = rumble_outer.winfo_height()

def _drag_motion(event):
    delta = _drag_start_y[0] - event.y_root
    new_h = max(120, _drag_start_h[0] + delta)
    rumble_outer.config(height=new_h)
    rumble_outer.grid_propagate(False)

drag_handle.bind("<ButtonPress-1>", _drag_start)
drag_handle.bind("<B1-Motion>",     _drag_motion)

# ── controls ──────────────────────────────────────────────────────────────────
rumble_frame = tk.Frame(rumble_outer, bg="#0d1a0d", bd=0)
rumble_frame.grid(row=1, column=0, sticky="nsew")
rumble_frame.grid_columnconfigure(3, weight=1)
rumble_frame.grid_rowconfigure(4, weight=1)

tk.Label(rumble_frame, text="🎥  Rumble Bot", bg="#0d1a0d", fg="#00ff88",
         font=("Segoe UI", 10, "bold")).grid(row=0, column=0, padx=(10, 6), pady=(6, 4), sticky="w")

# email
tk.Label(rumble_frame, text="Email:", bg="#0d1a0d", fg=text_fg,
         font=("Segoe UI", 9)).grid(row=0, column=1, sticky="e")
rumble_username_var = tk.StringVar(value=RUMBLE_USERNAME)
tk.Entry(rumble_frame, textvariable=rumble_username_var, width=14,
         bg="#1a2a1a", fg="#ffffff", insertbackground="#00ff88",
         relief="flat").grid(row=0, column=2, padx=(2, 8), sticky="ew")

# password
tk.Label(rumble_frame, text="Pass:", bg="#0d1a0d", fg=text_fg,
         font=("Segoe UI", 9)).grid(row=0, column=3, sticky="e", padx=(0, 2))
rumble_password_var = tk.StringVar()
tk.Entry(rumble_frame, textvariable=rumble_password_var, show="*", width=14,
         bg="#1a2a1a", fg="#ffffff", insertbackground="#00ff88",
         relief="flat").grid(row=0, column=4, padx=(2, 8), sticky="ew")

# model selector label
tk.Label(rumble_frame, text="Bot model:", bg="#0d1a0d", fg=text_fg,
         font=("Segoe UI", 9)).grid(row=0, column=5, sticky="e", padx=(0, 2))
rumble_model_var = tk.StringVar(value=re_engine.RUMBLE_MODEL)
tk.Entry(rumble_frame, textvariable=rumble_model_var, width=16,
         bg="#1a2a1a", fg="#ffffff", insertbackground="#00ff88",
         relief="flat").grid(row=0, column=6, padx=(2, 8), sticky="ew")

def _apply_rumble_model(*_):
    re_engine.RUMBLE_MODEL = rumble_model_var.get().strip()

rumble_model_var.trace_add("write", _apply_rumble_model)

# connect / disconnect / stop
connect_btn = tk.Button(
    rumble_frame, text="Connect", command=lambda: connect_rumble(),
    font=("Segoe UI", 9, "bold"), bg="#1a6e3a", fg="white",
    activebackground="#28a060", relief="flat", padx=10, pady=3
)
connect_btn.grid(row=0, column=7, padx=(0, 4), pady=(4, 2), sticky="ew")

disconnect_btn = tk.Button(
    rumble_frame, text="Disconnect", command=lambda: re_engine.disconnect(),
    font=("Segoe UI", 9), bg="#5a2020", fg="white",
    activebackground="#7a3030", relief="flat", padx=10, pady=3, state="disabled"
)
disconnect_btn.grid(row=0, column=8, padx=(0, 4), pady=(4, 2), sticky="ew")

rumble_stop_btn = tk.Button(
    rumble_frame, text="⏹ Stop Bot", command=lambda: re_engine.disconnect(),
    font=("Segoe UI", 9, "bold"), bg="#7a2020", fg="white",
    activebackground="#a03030", relief="flat", padx=10, pady=3, state="disabled"
)
rumble_stop_btn.grid(row=0, column=9, padx=(0, 10), pady=(4, 2), sticky="ew")

# stream URL
tk.Label(rumble_frame, text="Stream URL:", bg="#0d1a0d", fg=text_fg,
         font=("Segoe UI", 9)).grid(row=1, column=0, padx=(10, 4), pady=(0, 4), sticky="e")
rumble_url_var = tk.StringVar(value="https://rumble.com/live")
tk.Entry(rumble_frame, textvariable=rumble_url_var,
         bg="#1a2a1a", fg="#ffffff", insertbackground="#00ff88",
         relief="flat", width=42).grid(row=1, column=1, columnspan=5, padx=(0, 8), pady=(0, 4), sticky="ew")

# bot @name
tk.Label(rumble_frame, text="Bot @name:", bg="#0d1a0d", fg=text_fg,
         font=("Segoe UI", 9)).grid(row=1, column=6, padx=(0, 4), sticky="e")
rumble_botname_var = tk.StringVar(value=BOT_NAME)
tk.Entry(rumble_frame, textvariable=rumble_botname_var, width=18,
         bg="#1a2a1a", fg="#ffffff", insertbackground="#00ff88",
         relief="flat").grid(row=1, column=7, columnspan=2, padx=(0, 8), sticky="ew")

# char limit
tk.Label(rumble_frame, text="Char limit:", bg="#0d1a0d", fg=text_fg,
         font=("Segoe UI", 9)).grid(row=2, column=0, padx=(10, 4), pady=(0, 4), sticky="e")
rumble_char_limit_var = tk.StringVar(value=str(re_engine.RUMBLE_CHAR_LIMIT))
tk.Spinbox(
    rumble_frame, from_=100, to=5000, increment=10,
    textvariable=rumble_char_limit_var, width=7,
    bg="#1a2a1a", fg="#ffffff", insertbackground="#00ff88",
    buttonbackground="#1a3a1a", relief="flat", font=("Segoe UI", 9)
).grid(row=2, column=1, padx=(0, 4), pady=(0, 4), sticky="w")

_char_limit_feedback = tk.Label(rumble_frame, text="", bg="#0d1a0d", fg="#00ff88",
                                 font=("Segoe UI", 8, "italic"))
_char_limit_feedback.grid(row=2, column=3, columnspan=4, padx=(0, 10), pady=(0, 4), sticky="w")

def _apply_char_limit():
    try:
        val = int(rumble_char_limit_var.get().strip())
        if val < 50:
            messagebox.showwarning("Char Limit", "Minimum is 50.")
            return
        re_engine.RUMBLE_CHAR_LIMIT = val
        _char_limit_feedback.config(text=f"✔ Set to {val}", fg="#00ff88")
        root.after(2500, lambda: _char_limit_feedback.config(text=""))
    except ValueError:
        messagebox.showerror("Char Limit", "Please enter a whole number.")

tk.Button(rumble_frame, text="Set", command=_apply_char_limit,
          font=("Segoe UI", 9), bg="#1a4a2a", fg="white",
          activebackground="#28a060", relief="flat", padx=8, pady=2
          ).grid(row=2, column=2, padx=(0, 8), pady=(0, 4), sticky="w")

# status
rumble_status_label = tk.Label(
    rumble_frame, text="Disconnected", bg="#0d1a0d", fg="#888888",
    font=("Segoe UI", 8, "italic")
)
rumble_status_label.grid(row=3, column=0, columnspan=2, padx=10, pady=(0, 2), sticky="w")

# log + response viewer
log_response_frame = tk.Frame(rumble_frame, bg="#0d1a0d")
log_response_frame.grid(row=4, column=0, columnspan=10, sticky="nsew", padx=(6, 6), pady=(0, 6))
log_response_frame.grid_columnconfigure(0, weight=1)
log_response_frame.grid_columnconfigure(2, weight=1)
log_response_frame.grid_rowconfigure(1, weight=1)

tk.Label(log_response_frame, text="Bot Log", bg="#0d1a0d", fg="#00ff88",
         font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w", padx=(4, 0))
rumble_log_widget = scrolledtext.ScrolledText(
    log_response_frame, height=8, wrap=tk.WORD, state="disabled",
    font=("Consolas", 9), bg="#081508", fg="#88ff88",
    insertbackground="#00ff88", borderwidth=0, relief="flat"
)
rumble_log_widget.grid(row=1, column=0, sticky="nsew", padx=(4, 2))

tk.Frame(log_response_frame, bg="#1a3a1a", width=2).grid(row=0, column=1, rowspan=2, sticky="ns", padx=4)

tk.Label(log_response_frame, text="Last Full Response (read-only)", bg="#0d1a0d",
         fg="#ffcc44", font=("Segoe UI", 8, "bold")).grid(row=0, column=2, sticky="w", padx=(0, 4))
rumble_response_viewer = scrolledtext.ScrolledText(
    log_response_frame, height=8, wrap=tk.WORD, state="disabled",
    font=("Consolas", 9), bg="#0a0a18", fg="#d4d4d4",
    insertbackground="#d4d4d4", borderwidth=0, relief="flat"
)
rumble_response_viewer.grid(row=1, column=2, sticky="nsew", padx=(2, 4))

# =============================================================================
# RUMBLE ENGINE UI CALLBACKS
# =============================================================================
def _set_rumble_status(text: str):
    root.after(0, lambda: rumble_status_label.config(text=text))

def _update_response_viewer(text: str):
    def _do():
        rumble_response_viewer.config(state="normal")
        rumble_response_viewer.delete("1.0", tk.END)
        rumble_response_viewer.insert(tk.END, text)
        rumble_response_viewer.config(state="disabled")
    root.after(0, _do)

re_engine.register_ui_callbacks(
    set_status        = _set_rumble_status,
    update_viewer     = _update_response_viewer,
    enable_connect    = lambda: root.after(0, lambda: connect_btn.config(state="normal")),
    disable_connect   = lambda: root.after(0, lambda: connect_btn.config(state="disabled")),
    enable_disconnect = lambda: root.after(0, lambda: disconnect_btn.config(state="normal")),
    disable_disconnect= lambda: root.after(0, lambda: disconnect_btn.config(state="disabled")),
    enable_stop       = lambda: root.after(0, lambda: rumble_stop_btn.config(state="normal")),
    disable_stop      = lambda: root.after(0, lambda: rumble_stop_btn.config(state="disabled")),
)

# =============================================================================
# CONNECT / DISCONNECT  (UI side)
# =============================================================================
def _poll_rumble_log():
    try:
        while True:
            line = re_engine.rumble_state["log_queue"].get_nowait()
            rumble_log_widget.config(state="normal")
            rumble_log_widget.insert(tk.END, line + "\n")
            rumble_log_widget.see(tk.END)
            rumble_log_widget.config(state="disabled")
    except Empty:
        pass
    root.after(500, _poll_rumble_log)

def connect_rumble():
    if re_engine.rumble_state["running"]:
        messagebox.showinfo("Rumble Bot", "Bot is already running.")
        return
    username  = rumble_username_var.get().strip() or RUMBLE_USERNAME
    password  = rumble_password_var.get().strip() or RUMBLE_PASSWORD
    stream_url = rumble_url_var.get().strip()
    bot_name   = rumble_botname_var.get().strip() or BOT_NAME

    if not username:
        username = simpledialog.askstring("Rumble Login", "Rumble email address:")
        if not username:
            return
        rumble_username_var.set(username)

    if not password:
        password = simpledialog.askstring("Rumble Login", "Rumble password:", show="*")
        if not password:
            return

    if not stream_url:
        messagebox.showwarning("Rumble Bot", "Please enter the stream URL first.")
        return

    re_engine.connect(stream_url, username, password, bot_name)

# =============================================================================
# KEYBOARD SHORTCUTS
# =============================================================================
entry.bind("<Return>",       lambda e: (send_message(), "break"))
verse_entry.bind("<Return>", lambda e: (lookup_verse(),  "break"))

# =============================================================================
# START BACKGROUND LOADING  &  LOG POLLING
# =============================================================================
se.start_background_loading(status_callback=_status_callback)
root.after(500, _poll_rumble_log)

# =============================================================================
# MAINLOOP
# =============================================================================
root.mainloop()
