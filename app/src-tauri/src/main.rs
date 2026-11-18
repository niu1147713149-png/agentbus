// agentbus 桌面应用：
//   悬浮窗（hud）— 挂在屏幕顶部或侧边、永远置顶，派活时自己弹出来；
//   主窗口（main）— 基地 / 队员 / 技能 / 任务 / 战报 / 设置，平时藏着，从悬浮窗或菜单栏打开。
// 界面和数据都在本地服务（core/service.py，127.0.0.1:8790）里，Claude 右侧浏览器打开同一个地址。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, LogicalPosition, LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const ADDR: &str = "127.0.0.1:8790";
const URL: &str = "http://127.0.0.1:8790/";

fn http_get(path: &str) -> Option<String> {
    let mut s = TcpStream::connect_timeout(&ADDR.parse().ok()?, Duration::from_millis(400)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    write!(s, "GET {path} HTTP/1.0\r\nHost: {ADDR}\r\n\r\n").ok()?;
    let mut buf = String::new();
    s.read_to_string(&mut buf).ok()?;
    buf.split_once("\r\n\r\n").map(|(_, b)| b.to_string())
}

/// 打包好的服务程序（PyInstaller 文件夹版，作为应用资源放在 core-bin/ 里）。发布版用它，用户不用装 Python。
fn sidecar(app: &AppHandle) -> Option<PathBuf> {
    let name = if cfg!(windows) { "agentbus-core.exe" } else { "agentbus-core" };
    let p = app.path().resource_dir().ok()?.join("core-bin").join(name);
    p.exists().then_some(p)
}

/// 找 core/service.py：先看环境变量，再看 ~/agentbus/core，最后看应用自带的资源。
fn service_script(app: &AppHandle) -> Option<PathBuf> {
    let mut cands = vec![];
    if let Ok(p) = std::env::var("AGENTBUS_CORE") {
        cands.push(PathBuf::from(p).join("service.py"));
    }
    if let Some(home) = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }) {
        cands.push(PathBuf::from(home).join("agentbus").join("core").join("service.py"));
    }
    if let Ok(res) = app.path().resource_dir() {
        cands.push(res.join("core").join("service.py"));
    }
    cands.into_iter().find(|p| p.exists())
}

/// 用哪个 Python 跑源码版服务：Mac 用系统自带的；Windows 先试 py 启动器，再试 python。
fn python_cmd() -> Command {
    if cfg!(windows) {
        for (exe, pre) in [("py", vec!["-3"]), ("python", vec![]), ("python3", vec![])] {
            if Command::new(exe).args(&pre).arg("--version").stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false) {
                let mut c = Command::new(exe);
                c.args(pre);
                return c;
            }
        }
        Command::new("python")
    } else {
        Command::new("/usr/bin/python3")
    }
}

#[cfg(windows)]
fn no_window(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW：别弹黑框
}
#[cfg(not(windows))]
fn no_window(_cmd: &mut Command) {}

/// 服务没开就开一个。服务单独跑（不跟着应用退出），Claude 的浏览器和命令行随时能连。
fn ensure_service(app: &AppHandle) {
    if http_get("/api/health").is_some() {
        return;
    }
    // 先找源码（开发机上 ~/agentbus/core，改了代码立刻生效），没有再用应用里打包好的服务程序（发布版）
    let dev = service_script(app).filter(|p| !p.starts_with(app.path().resource_dir().unwrap_or_default()));
    let mut cmd = if let Some(script) = dev {
        let mut c = python_cmd();
        c.arg(&script).arg("8790");
        c
    } else if let Some(bin) = sidecar(app) {
        let mut c = Command::new(bin);
        c.args(["serve", "8790"]);
        if let Ok(res) = app.path().resource_dir() {
            c.env("AGENTBUS_UI", res.join("ui")); // 界面文件作为应用资源放着
        }
        c
    } else if let Some(script) = service_script(app) {
        let mut c = python_cmd();
        c.arg(&script).arg("8790");
        c
    } else {
        return;
    };
    // 开机启动要知道应用自己在哪
    if let Ok(exe) = std::env::current_exe() {
        cmd.env("AGENTBUS_APP_EXE", exe);
    }
    cmd.stdin(Stdio::null());
    no_window(&mut cmd);
    let log = std::env::temp_dir().join("agentbus-service.log");
    if let Ok(f) = std::fs::File::create(&log) {
        if let Ok(f2) = f.try_clone() {
            cmd.stdout(f).stderr(f2);
        }
    }
    let _ = cmd.spawn();
}

/// 打开主窗口：关掉时窗口是真的销毁了（网页进程攥着的立绘图层才会还给系统，藏起来不还），要用时再新建。
fn show_main(app: &AppHandle, page: Option<&str>) {
    let hash = page.map(|p| format!("#{}", p.replace('\'', "").trim_start_matches('#'))).unwrap_or_default();
    if let Some(w) = app.get_webview_window("main") {
        if !hash.is_empty() {
            let _ = w.eval(&format!("location.hash = '{}'", hash));
        }
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    let url: tauri::Url = format!("{URL}{hash}").parse().unwrap();
    if let Ok(w) = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
        .disable_drag_drop_handler() // Let the chat uploader receive HTML5 File objects, not local paths.
        .title("agentbus")
        .inner_size(1360.0, 900.0)
        .min_inner_size(420.0, 560.0)
        .build()
    {
        let _ = w.set_focus();
    }
}

/// 悬浮窗大小变了：按停靠位置重新摆。
/// top = 菜单栏下方，默认水平居中；right / left = 屏幕侧边，默认以收起时的胶囊为准垂直居中。
/// offset = 用户拖动后的偏移（顶部左右、侧边上下）；anchor = 侧边收起时窗口的高度。
/// 悬浮窗所在屏幕的可用区域（逻辑像素）：x, y, 宽, 高
fn work_area(win: &tauri::WebviewWindow) -> Option<(f64, f64, f64, f64)> {
    let mon = win.current_monitor().ok().flatten().or_else(|| win.primary_monitor().ok().flatten())?;
    let s = mon.scale_factor();
    let a = mon.work_area();
    Some((a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s))
}

/// 悬浮窗现在的位置和大小（拖动开始时用）
#[tauri::command]
fn hud_pos(app: AppHandle) -> Option<(f64, f64, f64, f64)> {
    let win = app.get_webview_window("hud")?;
    let s = win.scale_factor().ok()?;
    let p = win.outer_position().ok()?;
    let z = win.outer_size().ok()?;
    Some((p.x as f64 / s, p.y as f64 / s, z.width as f64 / s, z.height as f64 / s))
}

/// 拖动中：直接把窗口挪到 (x, y)
#[tauri::command]
fn hud_place(app: AppHandle, x: f64, y: f64) {
    if let Some(win) = app.get_webview_window("hud") {
        // 拖动时不让窗口出屏幕：出去了就找不到它在哪块屏幕上，之后再也摆不回来
        let (x, y) = match (hud_monitor(&win), win.outer_size()) {
            (Some((ax, ay, aw, ah, s)), Ok(z)) => {
                let (w, h) = (z.width as f64 / s, z.height as f64 / s);
                (x.max(ax).min(ax + aw - w.min(aw)), y.max(ay).min(ay + ah - h.min(ah)))
            }
            _ => (x, y),
        };
        let _ = win.set_position(LogicalPosition::new(x, y));
    }
}

/// 悬浮窗所在屏幕的可用区域（逻辑像素）+ 缩放；窗口跑到屏幕外时 current_monitor 是空的，退回主屏幕
fn hud_monitor(win: &tauri::WebviewWindow) -> Option<(f64, f64, f64, f64, f64)> {
    let mon = win.current_monitor().ok().flatten().or_else(|| win.primary_monitor().ok().flatten())?;
    let s = mon.scale_factor();
    let a = mon.work_area();
    Some((a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s, s))
}

/// 松手时决定停在哪：离某条屏幕边不到 48 像素就吸附过去（返回边和沿边的偏移），否则就地悬浮（返回中心点）
#[tauri::command]
fn hud_snap(app: AppHandle, x: f64, y: f64, w: f64, h: f64) -> Option<(String, f64, f64, f64)> {
    let win = app.get_webview_window("hud")?;
    let (ax, ay, aw, ah) = work_area(&win)?;
    let d = [("left", x - ax), ("right", ax + aw - (x + w)), ("top", y - ay), ("bottom", ay + ah - (y + h))];
    let (edge, dist) = d.iter().cloned().fold(("", f64::MAX), |m, e| if e.1 < m.1 { e } else { m });
    let (cx, cy) = (x + w / 2.0, y + h / 2.0);
    if dist < 48.0 {
        let off = if edge == "top" || edge == "bottom" { cx - (ax + aw / 2.0) } else { cy - (ay + ah / 2.0) };
        return Some((edge.to_string(), off, cx, cy));
    }
    Some(("float".into(), 0.0, cx, cy))
}

/// 指针所在那块屏幕的可用区域（拖动跨屏时用）：找不到就退回悬浮窗当前所在的屏幕
fn monitor_at(win: &tauri::WebviewWindow, px: f64, py: f64) -> Option<(f64, f64, f64, f64, f64)> {
    let mons = win.available_monitors().ok()?;
    for mon in mons {
        let s = mon.scale_factor();
        let (p, z) = (mon.position(), mon.size());
        let (x, y, w, h) = (p.x as f64 / s, p.y as f64 / s, z.width as f64 / s, z.height as f64 / s);
        if px >= x && px < x + w && py >= y && py < y + h {
            let a = mon.work_area();
            return Some((a.position.x as f64 / s, a.position.y as f64 / s, a.size.width as f64 / s, a.size.height as f64 / s, s));
        }
    }
    None
}

#[tauri::command]
fn hud_resize(app: AppHandle, w: f64, h: f64, dock: String, offset: Option<f64>, anchor: Option<f64>, cx: Option<f64>, cy: Option<f64>) {
    let Some(win) = app.get_webview_window("hud") else { return };
    // 拖动时带了指针位置：按指针所在的屏幕摆（能拖到第二块屏幕上）；平时按悬浮窗当前所在的屏幕
    let at = match (cx, cy) { (Some(x), Some(y)) => monitor_at(&win, x, y), _ => None };
    let Some((ax, ay, aw, ah, _)) = at.or_else(|| hud_monitor(&win)) else { return };
    let off = offset.unwrap_or(0.0);
    let h = h.min(ah - 16.0);
    let clamp = |v: f64, lo: f64, hi: f64| v.max(lo).min(hi.max(lo));
    let (x, y) = match dock.as_str() {
        "right" | "left" => {
            // 以收起时胶囊的中心为准上下对称展开，收起再缩回同一个中心
            let _ = anchor;
            let top = clamp(ay + ah / 2.0 + off - h / 2.0, ay + 8.0, ay + ah - h - 8.0);
            (if dock == "right" { ax + aw - w } else { ax }, top)
        }
        // 自由悬浮：以收起时胶囊的中心为准；在屏幕上半边往下展开，下半边往上展开
        "float" => {
            let (fx, fy) = (cx.unwrap_or(ax + aw / 2.0), cy.unwrap_or(ay + 80.0));
            let ph = anchor.unwrap_or(h);
            let top = if fy < ay + ah / 2.0 { fy - ph / 2.0 } else { fy + ph / 2.0 - h };
            (clamp(fx - w / 2.0, ax + 4.0, ax + aw - w - 4.0), clamp(top, ay + 4.0, ay + ah - h - 4.0))
        }
        // 底部：任务栏上方（Windows 常用）；work_area 已经扣掉任务栏
        "bottom" => (clamp(ax + (aw - w) / 2.0 + off, ax + 4.0, ax + aw - w - 4.0), ay + ah - h),
        _ => (clamp(ax + (aw - w) / 2.0 + off, ax + 4.0, ax + aw - w - 4.0), ay),
    };
    let _ = win.set_size(LogicalSize::new(w, h));
    let _ = win.set_position(LogicalPosition::new(x, y));
}

/// 自动弹出时拉到最上层（不抢键盘焦点）。
#[tauri::command]
fn hud_front(app: AppHandle) {
    if let Some(win) = app.get_webview_window("hud") {
        let _ = win.show();
        let _ = win.set_always_on_top(true);
        let _ = win.eval("window.__hudWake && window.__hudWake(true)"); // 从菜单栏叫出来：重新计空闲时间，并强制重新摆位置
    }
}

/// 悬浮窗透明留白处让鼠标穿过去，点得到下面的窗口；网页按光标落点决定开关。
#[tauri::command]
fn hud_through(app: AppHandle, on: bool) {
    if let Some(win) = app.get_webview_window("hud") {
        let _ = win.set_ignore_cursor_events(on);
    }
}

/// macOS 上悬浮窗不是当前窗口时网页收不到鼠标移入（要先点一下才灵），
/// 所以由这里每 60 毫秒读一次全局光标，落在悬浮窗范围里就把相对坐标告诉网页，移出去报一次 null。
/// 系统级“鼠标按下过几次”（左键 + 右键）：只看计数变化，按得再快也不会漏。悬浮窗不是当前窗口时，点别的应用网页收不到事件，靠它判断“点了外面”
#[cfg(target_os = "macos")]
fn press_count() -> u64 {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventSourceCounterForEventType(state: i32, event_type: u32) -> u32;
    }
    // kCGEventSourceStateCombinedSessionState = 0；kCGEventLeftMouseDown = 1，kCGEventRightMouseDown = 3
    unsafe { CGEventSourceCounterForEventType(0, 1) as u64 + CGEventSourceCounterForEventType(0, 3) as u64 }
}
#[cfg(target_os = "windows")]
fn press_count() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    #[link(name = "user32")]
    extern "system" {
        fn GetAsyncKeyState(key: i32) -> i16;
    }
    static N: AtomicU64 = AtomicU64::new(0);
    // 低位 = 上次查询以来按下过；VK_LBUTTON = 1，VK_RBUTTON = 2
    let hit = unsafe { (GetAsyncKeyState(1) & 1) != 0 || (GetAsyncKeyState(2) & 1) != 0 };
    if hit { N.fetch_add(1, Ordering::Relaxed) + 1 } else { N.load(Ordering::Relaxed) }
}
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn press_count() -> u64 {
    0
}

fn hover_loop(app: AppHandle) {
    let mut last: Option<(i32, i32)> = None;
    let mut presses = press_count();
    loop {
        std::thread::sleep(Duration::from_millis(60));
        let Some(win) = app.get_webview_window("hud") else { continue };
        if !win.is_visible().unwrap_or(false) {
            continue;
        }
        let (Ok(c), Ok(p), Ok(sz), Ok(k)) = (app.cursor_position(), win.outer_position(), win.outer_size(), win.scale_factor()) else { continue };
        let (x, y) = ((c.x - p.x as f64) / k, (c.y - p.y as f64) / k);
        let inside = x >= 0.0 && y >= 0.0 && x < sz.width as f64 / k && y < sz.height as f64 / k;
        let now = if inside { Some((x.round() as i32, y.round() as i32)) } else { None };
        let n = press_count();
        if n != presses {  // 按过鼠标：告诉网页按在哪（窗口外是 null），展开时点了面板以外就收起
            presses = n;
            let js = match now {
                Some((x, y)) => format!("window.__hudPress && window.__hudPress({x},{y})"),
                None => "window.__hudPress && window.__hudPress(null)".into(),
            };
            let _ = win.eval(&js);
        }
        if now == last {
            continue;
        }
        last = now;
        let js = match now {
            Some((x, y)) => format!("window.__hudHover && window.__hudHover({x},{y})"),
            None => "window.__hudHover && window.__hudHover(null)".into(),
        };
        let _ = win.eval(&js);
    }
}

#[tauri::command]
fn hud_visible(app: AppHandle, on: bool) {
    if let Some(win) = app.get_webview_window("hud") {
        let visible = win.is_visible().unwrap_or(false);
        if on && !visible {
            let _ = win.show();
        } else if !on && visible {
            let _ = win.hide();
        }
    }
}

#[tauri::command]
fn open_main(app: AppHandle, page: Option<String>) {
    show_main(&app, page.as_deref());
}

fn working_count(body: &str) -> usize {
    body.matches("\"state\": \"working\"").count()
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![hud_resize, hud_front, hud_visible, open_main, hud_pos, hud_place, hud_snap, hud_through])
        .setup(|app| {
            // 只在菜单栏和悬浮窗里出现，不占 Dock
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let handle = app.handle().clone();
            ensure_service(&handle);
            let hh = handle.clone();
            std::thread::spawn(move || hover_loop(hh));
            for _ in 0..25 {
                if http_get("/api/health").is_some() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(200));
            }

            // 主窗口不预先建（藏着也吃几百 MB），第一次打开时再建
            let url: tauri::Url = URL.parse().unwrap();

            WebviewWindowBuilder::new(app, "hud", WebviewUrl::External(url.join("hud.html").unwrap()))
                .title("agentbus 悬浮窗")
                .inner_size(260.0, 58.0)
                .decorations(false)
                .transparent(true)
                .shadow(false)
                .resizable(false)
                .always_on_top(true)
                .visible_on_all_workspaces(true)
                .skip_taskbar(true)
                .focused(false)
                .accept_first_mouse(true)
                .build()?;

            let open = MenuItem::with_id(app, "open", "打开基地", true, None::<&str>)?;
            let hud = MenuItem::with_id(app, "hud", "显示悬浮窗", true, None::<&str>)?;
            let browser = MenuItem::with_id(app, "browser", "在浏览器里打开", true, None::<&str>)?;
            let reload = MenuItem::with_id(app, "reload", "刷新界面", true, None::<&str>)?;
            let restart = MenuItem::with_id(app, "restart", "重启本地服务", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出 agentbus", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&open, &hud, &sep, &browser, &reload, &restart, &sep2, &quit])?;

            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png"))?;
            let tray = TrayIconBuilder::with_id("main")
                .icon(tray_icon)
                .icon_as_template(true)
                .tooltip("agentbus")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, e| match e.id.as_ref() {
                    "open" => show_main(app, None),
                    "hud" => hud_front(app.clone()),
                    "browser" => {
                        let opener = if cfg!(windows) { ("cmd", vec!["/c", "start", "", URL]) } else if cfg!(target_os = "macos") { ("open", vec![URL]) } else { ("xdg-open", vec![URL]) };
                        let mut c = Command::new(opener.0);
                        c.args(opener.1);
                        no_window(&mut c);
                        let _ = c.spawn();
                    }
                    "reload" => {
                        for label in ["main", "hud"] {
                            if let Some(w) = app.get_webview_window(label) {
                                let _ = w.eval("location.reload()");
                            }
                        }
                    }
                    "restart" => {
                        let _ = http_get("/api/shutdown"); // 让服务自己退出（各平台都一样）
                        std::thread::sleep(Duration::from_millis(800));
                        ensure_service(app);
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, e| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = e {
                        show_main(tray.app_handle(), None);
                        hud_front(tray.app_handle().clone()); // 顺便把悬浮窗叫出来（藏起来、缩成小球的都会醒）
                    }
                })
                .build(app)?;

            // 菜单栏图标旁显示几位在干活；服务挂了就拉起来，恢复后刷新两个窗口
            std::thread::spawn(move || {
                let mut was_down = false;
                loop {
                    match http_get("/api/state") {
                        Some(body) => {
                            if was_down {
                                for label in ["main", "hud"] {
                                    if let Some(w) = handle.get_webview_window(label) {
                                        let _ = w.eval("location.reload()");
                                    }
                                }
                                was_down = false;
                            }
                            let n = working_count(&body);
                            let _ = tray.set_title(if n > 0 { Some(format!("{n}")) } else { None::<String> });
                            let _ = tray.set_tooltip(Some(if n > 0 { format!("agentbus · {n} 位在干活") } else { "agentbus · 都闲着".into() }));
                        }
                        None => {
                            was_down = true;
                            ensure_service(&handle);
                        }
                    }
                    std::thread::sleep(Duration::from_secs(4));
                }
            });
            Ok(())
        })
        .on_window_event(|w, e| match e {
            // 关主窗口：真的关掉（销毁网页进程，省内存），下次打开再建；悬浮窗不能被关掉（在设置里隐藏）
            WindowEvent::CloseRequested { api, .. } if w.label() != "main" => {
                let _ = w.hide();
                api.prevent_close();
            }
            // 点了别的地方：悬浮窗自己收起
            WindowEvent::Focused(false) if w.label() == "hud" => {
                if let Some(wv) = w.app_handle().get_webview_window("hud") {
                    let _ = wv.eval("window.__hudBlur && window.__hudBlur()");
                }
            }
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("agentbus 启动失败")
        .run(|app, e| {
            // 点 Dock 图标重新打开主窗口：只有 macOS 有这个事件
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { .. } = e {
                show_main(app, None);
            }
            #[cfg(not(target_os = "macos"))]
            let _ = (app, e);
        });
}
