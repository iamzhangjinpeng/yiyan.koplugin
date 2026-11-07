"""Offline regression checks. Run: python -m unittest discover -s tests

Requires lupa (Lua 5.1); all KOReader/device/network APIs are replaced with
fixtures. No Hitokoto requests or Kindle files are touched.
"""
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

from lupa.lua51 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
PNG = (ROOT / "assets/lyrics-background.png").read_bytes()
BROKEN_PNG = b"\x89PNG\r\n\x1a\n" + b"fixture" * 4 + b"\0\0\0\0IEND\xaeB`\x82"

MOCKS = r'''
f = { queue = {}, requests = 0, renders = 0, live_widgets = 0, locks = 0,
    paints = {}, shapes = {}, now = 0, connected = true, settings = { data = {} },
    archive_entries = {} }
function f.settings:readSetting(k) return self.data[k] end
function f.settings:saveSetting(k, v) self.data[k] = v end
function f.settings:delSetting(k) self.data[k] = nil end
function f.settings:flush() end
G_reader_settings = f.settings
local ui = {}
function ui:scheduleIn(_, cb) table.insert(f.queue, cb) end
function ui:unschedule(cb)
    for i = #f.queue, 1, -1 do if f.queue[i] == cb then table.remove(f.queue, i) end end
end
function ui:show(message) f.message = message.text end
function ui:preventStandby() f.locks = f.locks + 1 end
function ui:allowStandby() f.locks = f.locks - 1 end
function ui:restartKOReader() f.restarted = true end
function f:drain()
    local steps = 0
    while #self.queue > 0 do
        steps = steps + 1
        assert(steps < 100, "scheduler failed to settle")
        table.remove(self.queue, 1)()
    end
    assert(self.locks == 0, "standby lock leaked")
end
local bb = {}
function bb.new(w, h)
    local image = { w = w, h = h }
    function image:getWidth() return self.w end
    function image:getHeight() return self.h end
    function image:free() assert(not self.freed); self.freed = true end
    function image:blitFrom() end
    function image:ditherblitFrom() self.dithered = true end
    function image:paintRect(x, y, w, h, color)
        assert(x >= 0 and y >= 0 and w > 0 and h > 0)
        assert(x + w <= self.w and y + h <= self.h)
        table.insert(f.shapes, { kind = "rect", x = x, y = y, w = w, h = h, color = color })
    end
    function image:paintCircle(x, y, radius, color)
        assert(x - radius >= 0 and y - radius >= 0 and x + radius < self.w and y + radius < self.h)
        table.insert(f.shapes, { kind = "circle", x = x, y = y, radius = radius, color = color })
    end
    function image:writeToFile(path, format)
        assert(format == "png")
        assert(self.dithered, "export must use 16-tone e-ink dithering")
        if f.fail_render then return false end
        local out = assert(io.open(path, "wb"))
        out:write(f.invalid_png or (f.truncate_png and "truncated" or test_png))
        out:close()
        if f.fail_encode then return true, false, "disk full" end
        f.renders = f.renders + 1
        f.export_width, f.export_height = self.w, self.h
        return true, true
    end
    return image
end
bb.TYPE_BB8, bb.COLOR_BLACK, bb.COLOR_WHITE = 1, "black", "white"
bb.COLOR_GRAY_4, bb.COLOR_GRAY_6, bb.COLOR_GRAY_B = "gray4", "gray6", "grayB"
local text = {}
function text:new(t)
    f.live_widgets = f.live_widgets + 1
    local rows = 0
    for line in (t.text .. "\n"):gmatch("(.-)\n") do
        local chars = 0
        for i = 1, #line do
            local b = line:byte(i)
            if b < 128 or b >= 192 then chars = chars + 1 end
        end
        rows = rows + math.max(1, math.ceil(chars * t.face.size / t.width))
    end
    local line_height = math.floor(t.face.size * (1 + t.line_height) + 0.5)
    local height = rows * line_height
    if t.height then
        local page = math.max(1, math.floor(t.height / line_height))
        if t.height_overflow_show_ellipsis and rows > page then t.line_with_ellipsis = page end
        height = t.height_adjust and math.min(rows, page) * line_height or math.max(t.height, line_height)
    end
    f.max_text_buffer_pixels = math.max(f.max_text_buffer_pixels or 0, t.width * height)
    function t:getSize() return { w = self.width, h = height } end
    function t:paintTo(_, x, y)
        assert(self:getSize().h > 0)
        table.insert(f.paints, { x = x, y = y, w = self.width, h = self:getSize().h,
            size = self.face.size, bold = self.bold, line_height = self.line_height, alignment = self.alignment,
            fg = self.fgcolor, bg = self.bgcolor, text = self.text })
    end
    function t:free() assert(not self.freed); self.freed = true; f.live_widgets = f.live_widgets - 1 end
    return t
end
local ffi = {}
function ffi.runInSubProcess(task)
    if f.spawn_failed then return false, "fork failed" end
    f.done = not f.hung
    task(1, 1)
    if f.spawn_hook_error then error("after-fork hook failed") end
    return 1, 1
end
function ffi.writeToFD(_, value) f.pipe = value end
function ffi.readAllFromFD() return f.pipe end
function ffi.isSubProcessDone() return f.done end
function ffi.terminateSubProcess() f.done = true; f.kills = (f.kills or 0) + 1 end
function ffi.copyFile(a, b)
    f.copy_calls = (f.copy_calls or 0) + 1
    if f.fail_copy_at == f.copy_calls then return false end
    local src = assert(io.open(a, "rb")); local data = src:read("*a"); src:close()
    local out = assert(io.open(b, "wb")); out:write(data); out:close()
end
local http = {}
function http.open()
    return { close = function() f.closed_connections = (f.closed_connections or 0) + 1 end }
end
function http.request(request)
    local opened, connection = pcall(http.open)
    if not opened then return nil, connection end
    f.request_url = request.url
    assert(request.method == "GET")
    local data, code
    if request.url:match("^https://api%.github%.com/") then
        f.update_checks = (f.update_checks or 0) + 1
        assert(request.redirect == false)
        data, code = f.update_release_body or f.raw_response, f.update_api_code or 200
    elseif request.url:match("^https://github%.com/iamzhangjinpeng/yiyan%.koplugin/releases/download/") then
        f.update_downloads = (f.update_downloads or 0) + 1
        assert(request.redirect == true)
        data, code = f.update_package_data or "package", f.update_download_code or 200
    else
        f.requests = f.requests + 1
        assert(request.redirect == false, "a redirect must not spend another request")
        data, code = f.raw_response, f.http_code or 200
    end
    if f.http_error then return nil, "offline" end
    local ok, err = request.sink(data)
    connection:close()
    if not ok then return nil, err end
    return 1, code
end
local confirm = {}
function confirm:new(options) f.confirm = options; return options end
local archiver = { Reader = {} }
function archiver.Reader:new()
    local reader = {}
    function reader:open(path) f.archive_path = path; return not f.archive_open_failed end
    function reader:iterate()
        local i = 0
        return function()
            i = i + 1
            local entry = f.archive_entries[i]
            if entry then return entry end
        end
    end
    function reader:extractToPath(path, destination)
        if f.archive_fail_path == path then self.err = "extract_failed"; return false end
        local data = py_archive_data(path)
        if not data then self.err = "missing_archive_data"; return false end
        local file = io.open(destination, "wb")
        if not file then self.err = "cannot_write_archive_entry"; return false end
        file:write(data); file:close(); return true
    end
    function reader:close() end
    return reader
end
local modules = {
    ["ui/widget/container/widgetcontainer"] = { extend = function(_, t) return t end },
    ["ui/uimanager"] = ui,
    ["ui/widget/infomessage"] = { new = function(_, t) return t end },
    ["ui/widget/inputdialog"] = {},
    ["ui/widget/confirmbox"] = confirm,
    ["ui/widget/textboxwidget"] = text,
    ["ui/font"] = { getFace = function(_, _, size)
        return { size = math.ceil(size * (f.screen_scale or 1)) }
    end },
    ["ffi/blitbuffer"] = bb,
    ["ffi"] = { C = { free = function(data) assert(data == "decoded") end } },
    ["ffi/png"] = { decodeFromFile = function(path, components)
        assert(components == 1)
        local file = io.open(path, "rb")
        if not file then return false end
        local data = file:read("*a")
        file:close()
        if data == test_png or data == alternate_test_png or data == date_png or data == dark_png then
            return true, { data = "decoded" }
        end
        return false, "invalid PNG"
    end },
    ["ui/renderimage"] = {
        renderImageFile = function(_, path)
            local name = path:match("/([^/]+)$")
            assert(path == test_root .. "/assets/" .. name)
            local classic = name == "lyrics-background.png"
            assert(classic or name == "date-card-background.png" or name == "dark-card-background.png")
            local file = assert(io.open(path, "rb"))
            file:close()
            f.background = name
            if f.missing_background then return nil end
            return classic and bb.new(758, 1024) or bb.new(1072, 1448)
        end,
        scaleBlitBuffer = function(_, image, w, h) image:free(); return bb.new(w, h) end,
    },
    ["device"] = { screen = {
        getWidth = function() return f.screen_width or 758 end,
        getHeight = function() return f.screen_height or 1024 end,
        scaleBySize = function(_, size) return math.ceil(size * (f.screen_scale or 1)) end,
    } },
    ["util"] = { makePath = function(path) py_mkdir(path); return true end },
    ["ffi/util"] = ffi,
    ["ffi/archiver"] = archiver,
    ["socket.http"] = http,
    ["socketutil"] = { set_timeout = function() end },
    ["ui/network/manager"] = {
        isConnected = function() return f.connected end,
        isWifiOn = function() return f.wifi_on end,
    },
    ["ui/time"] = { to_s = function(v) return v end, boottime_or_monotonic = function() return f.now end },
    ["logger"] = { warn = function() end },
    ["json"] = { encode = py_encode, decode = py_decode },
    ["ffi/sha2"] = { md5 = function(data)
        return (data:gsub(".", function(c) return string.format("%02x", c:byte()) end))
    end },
    ["gettext"] = function(v) return v end,
    ["libs/libkoreader-lfs"] = {
        attributes = py_mode,
        symlinkattributes = py_symlink_mode,
        mkdir = py_mkdir_exclusive,
        rmdir = py_rmdir,
        dir = function(path)
            local names, i = py_listdir(path), 0
            return function() i = i + 1; return names[i] end
        end,
    },
}
for name, module in pairs(modules) do package.loaded[name] = module end
local input = assert(io.open(test_root .. "/main.lua", "rb"))
local source = input:read("*a"); input:close()
source = source:gsub('local SAVE_DIR = "[^"]+"', 'local SAVE_DIR = ' .. string.format("%q", test_dir .. "/"))
function loadPlugin()
    return assert(loadstring(source, "@" .. test_root .. "/main.lua"))()
end
plugin = loadPlugin()
function newPlugin()
    return setmetatable({ path = test_root, ui = { menu = { registerToMainMenu = function() end } } }, { __index = plugin })
end
obj = newPlugin()
wallpaper = dofile(test_root .. "/wallpaper.lua")
updater = dofile(test_root .. "/updater.lua")
'''


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yiyan-check-")
        self.path = Path(self.tmp.name)
        self.archive_files = {}
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        g = self.lua.globals()
        g.test_root, g.test_dir = str(ROOT), str(self.path)
        g.test_png = PNG
        g.date_png = (ROOT / "assets/date-card-background.png").read_bytes()
        g.dark_png = (ROOT / "assets/dark-card-background.png").read_bytes()
        g.py_mkdir = lambda p: Path(p).mkdir(parents=True, exist_ok=True)
        g.py_mode = lambda p, _: "directory" if Path(p).is_dir() else "file" if Path(p).is_file() else None
        g.py_symlink_mode = lambda p, key: "link" if Path(p).is_symlink() else g.py_mode(p, key)
        g.py_mkdir_exclusive = self.mkdir_exclusive
        g.py_rmdir = lambda p: Path(p).rmdir()
        g.py_listdir = lambda p: self.lua.table_from(sorted(x.name for x in Path(p).iterdir()))
        g.py_encode = lambda v: json.dumps(self.to_python(v), ensure_ascii=False)
        g.py_decode = lambda s: self.to_lua(json.loads(s))
        g.py_archive_data = lambda path: self.archive_files.get(path)
        self.lua.execute(MOCKS)
        g.test_clock = int(datetime(2026, 11, 4, 8, tzinfo=timezone.utc).timestamp())
        self.lua.execute("os.time = function() return test_clock end")
        self.f, self.obj = g.f, g.obj
        self.payload = {"hitokoto": "用代码表达言语的魅力，用代码书写山河的壮丽。", "from": "一言开发者中心", "from_who": "一言"}
        self.f.raw_response = json.dumps(self.payload, ensure_ascii=False)
        self.date = self.lua.eval('os.date("!%Y%m%d", os.time() + 8 * 3600)')

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def mkdir_exclusive(path):
        try:
            Path(path).mkdir()
        except OSError:
            return False
        return True

    def to_python(self, value):
        if hasattr(value, "items"):
            return {k: self.to_python(v) for k, v in value.items()}
        return value

    def to_lua(self, value):
        if isinstance(value, dict):
            return self.lua.table_from({k: self.to_lua(v) for k, v in value.items()})
        if isinstance(value, list):
            return self.lua.table_from([self.to_lua(v) for v in value])
        return value

    def refresh(self, fetch_new=False):
        self.obj.refreshDaily(self.obj, True, fetch_new)
        self.f.drain(self.f)

    def cache(self, version=None):
        record = {"date": self.date, "payload": self.payload}
        if version is not None: record["layout_version"] = version
        (self.path / (self.date + ".json")).write_text(json.dumps(record, ensure_ascii=False))

    def test_success_is_requested_only_once_even_after_settings_reset(self):
        self.refresh()
        self.assertEqual(self.f.request_url, "https://v1.hitokoto.cn/?encode=json&charset=utf-8")
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.assertEqual(json.loads((self.path / (self.date + ".json")).read_text())["layout_version"], 7)
        self.f.settings.data = self.lua.table()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertEqual(self.f.live_widgets, 0)

    def cover_menu(self):
        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        return next(item.sub_item_table for _, item in menu.yiyan.sub_item_table.items()
                    if item.text == "封面类型")

    def update_menu_item(self):
        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        return next(item for _, item in menu.yiyan.sub_item_table.items()
                    if item.text == "检查在线更新")

    def prepare_update_package(self, version="1.5.0", omit=None, bad_path=None):
        files = {}
        installed_version = str(self.lua.globals().updater.VERSION)
        for relative in self.lua.globals().updater.FILES.values():
            path = ROOT / relative
            data = path.read_bytes()
            if relative in {"_meta.lua", "updater.lua"}:
                data = data.replace(('"' + installed_version + '"').encode(), ('"' + version + '"').encode())
            files["yiyan.koplugin/" + relative] = data
        self.archive_files = files
        entries = []
        for path, data in files.items():
            if path == omit:
                continue
            entries.append({"path": path, "mode": "file", "size": len(data)})
        if bad_path:
            entries.append({"path": bad_path, "mode": "file", "size": 1})
            self.archive_files[bad_path] = b"x"
        self.f.archive_entries = self.lua.table_from(entries)
        self.f.update_package_data = b"fixture"
        return files

    def release_payload(self, version="1.5.0", size=None):
        tag = "v" + version
        name = f"yiyan.koplugin-{tag}-install.zip"
        url = f"https://github.com/iamzhangjinpeng/yiyan.koplugin/releases/download/{tag}/{name}"
        return {"tag_name": tag, "draft": False, "prerelease": False,
                "assets": [{"name": name, "browser_download_url": url,
                            "size": size if size is not None else len(self.f.update_package_data or b"fixture")}]}

    def test_updater_compares_numeric_versions_and_rejects_untrusted_release_urls(self):
        updater = self.lua.globals().updater
        self.assertIn(f'version = "{updater.VERSION}"', (ROOT / "_meta.lua").read_text())
        self.assertTrue(updater.isNewer("1.10.0", "1.9.9"))
        self.assertFalse(updater.isNewer("1.4.0", "1.4.0"))
        self.assertFalse(updater.isNewer("1.5.0-beta", "1.4.0"))
        release = self.to_lua(self.release_payload())
        update = updater.releaseToInstall(release, "1.4.0")
        self.assertEqual(update.version, "1.5.0")
        release.assets[1].browser_download_url = "https://example.com/untrusted.zip"
        update, reason = updater.releaseToInstall(release, "1.4.0")
        self.assertIsNone(update)
        self.assertEqual(reason, "missing_asset")

    def test_update_package_rejects_path_traversal(self):
        self.prepare_update_package(bad_path="../../outside.lua")
        stage_dir = self.path / "staging"
        stage_dir.mkdir()
        ok, reason = self.lua.globals().updater.extractPackage(
            "fixture.zip", str(stage_dir), "1.5.0", self.lua.globals().wallpaper.isValidPNG)
        self.assertFalse(ok)
        self.assertEqual(reason, "unexpected_archive_entry")
        self.assertFalse((self.path / "outside.lua").exists())

    def test_update_menu_checks_github_only_after_manual_request_and_respects_wifi(self):
        item = self.update_menu_item()
        self.f.connected = False
        item.callback()
        self.assertIn("网络未连接", self.f.message)
        self.assertFalse(self.f.update_checks or 0)
        self.assertFalse(self.f.requests)

        self.f.connected = True
        self.f.update_release_body = json.dumps({"tag_name": "v1.4.0", "assets": []})
        item.callback()
        self.f.drain(self.f)
        self.assertEqual(self.f.update_checks, 1)
        self.assertEqual(self.f.requests, 0)
        self.assertIn("已是最新版", self.f.message)

    def test_online_update_downloads_validates_installs_and_prompts_for_restart(self):
        files = self.prepare_update_package()
        install_dir = self.path / "plugins" / "yiyan.koplugin"
        (install_dir / "assets").mkdir(parents=True)
        for relative in self.lua.globals().updater.FILES.values():
            destination = install_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"old plugin file")
        self.obj.path = str(install_dir)
        self.lua.execute(r'''
            f.update_install_order = {}
            local rename = os.rename
            os.rename = function(source, destination)
                if destination:match("/plugins/yiyan%.koplugin/") then
                    table.insert(f.update_install_order, destination:match("([^/]+)$"))
                end
                return rename(source, destination)
            end
        ''')
        payload = self.release_payload()
        self.f.update_release_body = json.dumps(payload)

        self.update_menu_item().callback()
        self.f.drain(self.f)
        self.assertIn("v1.5.0", self.f.confirm.text)
        self.assertEqual(self.f.update_checks, 1)
        self.assertEqual(self.f.update_downloads or 0, 0)

        self.f.confirm.ok_callback()
        self.f.drain(self.f)
        self.assertEqual(self.f.update_downloads, 1)
        self.assertIn("已更新到 v1.5.0", self.f.confirm.text)
        self.assertFalse(self.f.restarted or False)
        for archive_path, data in files.items():
            relative = archive_path.removeprefix("yiyan.koplugin/")
            self.assertEqual((install_dir / relative).read_bytes(), data)
        self.assertEqual(list(self.f.update_install_order.values()), [
            "lyrics-background.png", "date-card-background.png", "dark-card-background.png",
            "wallpaper.lua", "updater.lua", "main.lua", "_meta.lua",
        ])

        self.f.confirm.ok_callback()
        self.assertTrue(self.f.restarted)

    def test_online_update_rejects_incomplete_package_without_changing_installed_files(self):
        self.prepare_update_package(omit="yiyan.koplugin/updater.lua")
        install_dir = self.path / "plugins" / "yiyan.koplugin"
        (install_dir / "assets").mkdir(parents=True)
        before = {}
        for relative in self.lua.globals().updater.FILES.values():
            destination = install_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"old " + relative.encode())
            before[relative] = destination.read_bytes()
        self.obj.path = str(install_dir)
        self.f.update_release_body = json.dumps(self.release_payload())

        self.update_menu_item().callback()
        self.f.drain(self.f)
        self.f.confirm.ok_callback()
        self.f.drain(self.f)
        self.assertIn("校验失败", self.f.message)
        self.assertFalse(self.f.restarted or False)
        for relative, data in before.items():
            self.assertEqual((install_dir / relative).read_bytes(), data)

    def test_failed_update_replacement_rolls_back_already_replaced_files(self):
        files = self.prepare_update_package()
        install_dir = self.path / "plugins" / "yiyan.koplugin"
        (install_dir / "assets").mkdir(parents=True)
        stage_dir = self.path / "stage"
        for archive_path, data in files.items():
            relative = archive_path.removeprefix("yiyan.koplugin/")
            staged = stage_dir / "yiyan.koplugin" / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
            destination = install_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"old " + relative.encode())
        old_files = {relative: (install_dir / relative).read_bytes()
                     for relative in self.lua.globals().updater.FILES.values()}
        self.lua.globals().test_install_dir = str(install_dir)
        self.lua.globals().test_stage_dir = str(stage_dir)
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(source, destination)
                if source:match("/updater%.lua$") then return nil, "fixture rename failure" end
                return rename(source, destination)
            end
            local updater = dofile(test_root .. "/updater.lua")
            local ffi = package.loaded["ffi/util"]
            rollback_ok, rollback_error = updater.install(test_install_dir, test_stage_dir, ffi.copyFile)
            os.rename = rename
        ''')
        self.assertFalse(self.lua.globals().rollback_ok)
        self.assertEqual(self.lua.globals().rollback_error, "cannot_replace_install")
        for relative, data in old_files.items():
            self.assertEqual((install_dir / relative).read_bytes(), data)

    def test_cover_menu_switches_and_persists_styles_without_refetching(self):
        self.f.screen_width, self.f.screen_height = 1072, 1448
        covers = self.cover_menu()
        self.assertEqual([covers[i].text for i in range(1, len(covers) + 1)],
                         ["原版半露 CD（默认）", "白底日期卡", "纯色深灰卡"])
        self.assertTrue(covers[1].checked_func())
        covers[2].callback()
        self.assertEqual(self.f.requests, 0)
        self.assertIn("封面类型已保存", self.f.message)

        self.refresh()
        cache = self.path / (self.date + ".json")
        self.assertEqual(self.f.background, "date-card-background.png")
        self.assertEqual((self.f.export_width, self.f.export_height), (1072, 1448))
        self.assertEqual(json.loads(cache.read_text())["cover_style"], "calendar")
        self.assertTrue({"Nov.", "04", "星期三"} <= {p.text for _, p in self.f.paints.items()})

        self.f.paints = self.lua.table()
        self.f.connected = False
        covers[3].callback()
        self.assertEqual(self.f.background, "dark-card-background.png")
        self.assertEqual(json.loads(cache.read_text())["cover_style"], "dark")
        self.assertEqual(self.f.settings.data.yiyan_cover_style, "dark")
        self.assertIn("11.04  /  WED", {p.text for _, p in self.f.paints.items()})
        self.assertTrue(all(p.bg == "gray4" for _, p in self.f.paints.items()))

        self.lua.execute("plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        covers = self.cover_menu()
        self.assertTrue(covers[3].checked_func())
        covers[1].callback()
        self.assertEqual(self.f.background, "lyrics-background.png")
        self.assertEqual(json.loads(cache.read_text())["payload"]["hitokoto"], self.payload["hitokoto"])
        self.assertEqual(self.f.requests, 1)
        self.assertEqual(self.f.live_widgets, 0)

    def test_switching_style_recovers_an_interrupted_render_for_another_style(self):
        self.refresh()
        cache = self.path / (self.date + ".json")
        new_png = (ROOT / "docs/demo.png").read_bytes()
        self.lua.globals().alternate_test_png = new_png
        self.f.invalid_png = new_png
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if f.fail_cache and dst:match("%d+%.json$") then
                    f.cache_renames = (f.cache_renames or 0) + 1
                    if f.cache_renames == 2 then return nil, "disk full" end
                end
                return rename(src, dst)
            end
        ''')
        covers = self.cover_menu()
        self.f.fail_cache = True
        covers[2].callback()
        self.assertEqual(json.loads(cache.read_text())["pending_image"]["cover_style"], "calendar")
        self.assertEqual(cache.with_suffix(".png").read_bytes(), new_png)
        self.assertEqual((self.path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)

        self.f.fail_cache, self.f.invalid_png = False, None
        covers[3].callback()
        record = json.loads(cache.read_text())
        self.assertEqual(record["cover_style"], "dark")
        self.assertNotIn("pending_image", record)
        self.assertEqual(cache.with_suffix(".png").read_bytes(), PNG)
        self.assertEqual(self.f.background, "dark-card-background.png")
        self.assertEqual(self.f.requests, 1)

    def test_calendar_normalizes_line_endings_and_bounds_long_text_buffers(self):
        self.f.screen_width, self.f.screen_height = 1072, 1448
        quote = "第一行\r\n第二行\r第三行\n" + "下一行\n" * 600
        self.f.raw_response = json.dumps({**self.payload, "hitokoto": quote}, ensure_ascii=False)
        self.cover_menu()[2].callback()
        self.refresh()
        rendered = next(p.text for _, p in self.f.paints.items() if p.text.startswith("第一行"))
        self.assertTrue(rendered.startswith("第一行\n第二行\n第三行\n"))
        self.assertNotIn("\r", rendered)
        self.assertLessEqual(self.f.max_text_buffer_pixels, 1072 * 300)
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.assertEqual(self.f.live_widgets, 0)

    def test_manual_menu_requests_another_sentence_and_keeps_wallpaper_on_failure(self):
        self.refresh()
        cache = self.path / (self.date + ".json")
        display = self.path / ".yiyan-screen/wallpaper.png"
        old_cache = cache.read_bytes()
        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        entries = [item for _, item in menu.yiyan.sub_item_table.items() if "生成" in item.text]
        self.assertEqual([item.text for item in entries], ["重新生成今日壁纸"])
        self.f.http_error = True
        entries[0].callback()
        self.f.drain(self.f)
        self.assertEqual(self.f.requests, 2)
        self.assertIn("可手动立即重试", self.f.message)
        self.assertEqual(cache.read_bytes(), old_cache)
        self.assertEqual(display.read_bytes(), PNG)

        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertIn("已有今日句子", self.obj.networkDiagnostics(self.obj))
        self.assertIn("可手动立即重试", self.obj.networkDiagnostics(self.obj))
        new_png = (ROOT / "docs/demo.png").read_bytes()
        self.lua.globals().alternate_test_png = self.f.invalid_png = new_png
        self.f.raw_response = json.dumps({**self.payload, "hitokoto": "手动获取的新句子"})
        self.f.http_error = False
        entries[0].callback()
        self.f.drain(self.f)
        self.assertEqual(self.f.requests, 3)
        self.assertEqual(json.loads(cache.read_text())["payload"]["hitokoto"], "手动获取的新句子")
        self.assertEqual(display.read_bytes(), new_png)
        self.refresh()
        self.assertEqual(self.f.requests, 3)

    def test_manual_menu_bypasses_normal_retry_interval_but_automatic_retry_waits(self):
        self.f.http_error = True
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.f.connected = False
        self.assertIn("联网后可手动立即重试", self.obj.networkDiagnostics(self.obj))
        self.f.connected = True

        # An automatic check remains subject to the five-minute retry window.
        self.refresh()
        self.assertEqual(self.f.requests, 1)

        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        manual = next(item.callback for _, item in menu.yiyan.sub_item_table.items()
                      if item.text == "重新生成今日壁纸")
        self.f.http_error = False
        manual()
        self.f.drain(self.f)
        self.assertEqual(self.f.requests, 2)
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_manual_response_recovers_after_cache_promotion_failure_and_restart(self):
        self.refresh()
        cache = self.path / (self.date + ".json")
        display = self.path / ".yiyan-screen/wallpaper.png"
        old_cache = cache.read_bytes()
        self.f.raw_response = json.dumps({**self.payload, "hitokoto": "已暂存的新句子"})
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if f.fail_cache and dst:match("%d+%.json$") then return nil, "disk full" end
                return rename(src, dst)
            end
        ''')
        self.f.fail_cache = True
        self.refresh(True)
        self.assertEqual(cache.read_bytes(), old_cache)
        self.assertEqual(display.read_bytes(), PNG)
        self.assertIn("新句子已收到", self.obj.networkDiagnostics(self.obj))
        self.lua.execute("plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        self.f.connected = self.f.fail_cache = False
        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertEqual(json.loads(cache.read_text())["payload"]["hitokoto"], "已暂存的新句子")
        self.assertFalse(list(self.path.glob(".yiyan-work-*")))

    def test_cancelled_manual_response_does_not_replace_cached_wallpaper_after_restart(self):
        self.refresh()
        cache = self.path / (self.date + ".json")
        old_cache = cache.read_bytes()
        self.f.hung = True
        self.f.raw_response = json.dumps({**self.payload, "hitokoto": "被取消的句子"})
        self.obj.refreshDaily(self.obj, True, True)
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if dst:match("%.attempt$") then return nil, "unwritable" end
                return rename(src, dst)
            end
        ''')
        self.obj.stopPlugin(self.obj)
        self.assertTrue(list(self.path.glob(".yiyan-work-*/data")))
        self.lua.execute("f.queue = {}; f.locks = 0; plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        self.refresh()
        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertEqual(cache.read_bytes(), old_cache)

    def test_diagnostics_menu_checks_wifi_without_sending_or_scheduling_requests(self):
        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        diagnostics = next(item.callback for _, item in menu.yiyan.sub_item_table.items()
                           if item.text == "网络诊断")
        self.f.connected, self.f.wifi_on = False, False
        diagnostics()
        self.assertIn("Wi-Fi  已关闭", self.f.message)
        self.assertIn("等待联网", self.f.message)
        self.f.wifi_on = True
        diagnostics()
        self.assertIn("已开启，尚未连通", self.f.message)
        self.f.connected = True
        diagnostics()
        self.assertIn("已连接本地网络", self.f.message)
        self.assertIn("等待下次检查", self.f.message)
        self.assertEqual(self.f.requests, 0)
        self.assertEqual(len(self.f.queue), 0)
        self.assertFalse(list(self.path.iterdir()))

    def test_diagnostics_reports_request_progress_cooldown_and_success(self):
        self.f.http_code = 429
        self.obj.refreshDaily(self.obj, False)
        self.assertIn("后台请求进行中", self.obj.networkDiagnostics(self.obj))
        self.f.drain(self.f)
        failed = self.obj.networkDiagnostics(self.obj)
        self.assertIn("服务限流（60 分钟后可重试）", failed)
        self.assertIn("最近请求  服务限流（HTTP 429）", failed)
        self.assertEqual(self.f.requests, 1)
        self.lua.execute("plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        self.assertIn("服务限流（60 分钟后可重试）", self.obj.networkDiagnostics(self.obj))
        self.lua.execute("test_clock = test_clock + 3600")
        self.f.http_code = 200
        self.refresh()
        complete = self.obj.networkDiagnostics(self.obj)
        self.assertIn("今日句子  已缓存", complete)
        self.assertIn("今日壁纸  已就绪", complete)
        self.assertIn("今日无需自动请求", complete)
        self.assertIn("最近请求  成功收到句子", complete)
        self.assertEqual(self.f.requests, 2)

    def test_category_menu_updates_the_next_request_only(self):
        self.lua.globals().test_time = int(datetime(2026, 11, 4, 8, tzinfo=timezone.utc).timestamp())
        self.lua.execute("os.time = function() return test_time end")
        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        categories = menu.yiyan.sub_item_table[2].sub_item_table
        self.assertEqual([categories[i].text for i in range(1, len(categories) + 1)], [
            "默认随机", "动画", "漫画", "游戏", "文学", "原创", "来自网络", "其他",
            "影视", "诗词", "网易云", "哲学", "抖机灵",
        ])
        self.assertTrue(categories[1].checked_func())
        categories[11].callback()
        categories[5].callback()
        self.assertFalse(categories[1].checked_func())
        self.assertTrue(categories[11].checked_func())
        self.assertTrue(categories[5].checked_func())
        self.f.raw_response = json.dumps({
            **self.payload, "type": "j", "from_who": None, "uuid": "75a45fd4-4f2f-45eb-80cb-6f0a7bcdfaf2",
        })
        self.refresh()
        self.assertEqual(self.f.request_url, "https://v1.hitokoto.cn/?encode=json&charset=utf-8&c=d&c=j")
        payload = json.loads((self.path / "20261104.json").read_text())["payload"]
        self.assertEqual(payload["type"], "j")
        self.assertEqual(payload["from_who"], "")
        self.assertEqual(payload["uuid"], "75a45fd4-4f2f-45eb-80cb-6f0a7bcdfaf2")

        categories[5].callback()
        categories[10].callback()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.lua.execute("test_time = test_time + 86400")
        self.obj = self.lua.globals().newPlugin()
        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertEqual(self.f.request_url, "https://v1.hitokoto.cn/?encode=json&charset=utf-8&c=i&c=j")
        categories[1].callback()
        self.assertTrue(categories[1].checked_func())
        self.lua.execute("test_time = test_time + 86400")
        self.refresh()
        self.assertEqual(self.f.request_url, "https://v1.hitokoto.cn/?encode=json&charset=utf-8")

    def test_failed_request_can_succeed_later_same_day_after_restart(self):
        self.f.http_error = True
        self.refresh()
        self.f.settings.data = self.lua.table()
        self.lua.execute("plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        self.f.http_error = False
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.lua.execute("test_clock = test_clock + 300")
        self.refresh()
        self.lua.execute("test_clock = test_clock + 300")
        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_legacy_failed_request_record_does_not_block_today(self):
        settings = self.f.settings.data
        settings.yiyan_last_attempt_date = self.date
        settings.yiyan_attempt_history = self.lua.table_from({self.date: True})
        (self.path / (self.date + ".attempt")).write_text(self.date + "\n.yiyan-work-123-1\n")
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_rate_limiting_waits_an_hour_even_after_directory_change(self):
        self.f.http_code = 429
        self.refresh()
        self.assertIn("一小时", self.f.message)
        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        manual = next(item.callback for _, item in menu.yiyan.sub_item_table.items()
                      if item.text == "重新生成今日壁纸")
        manual()
        self.assertEqual(self.f.requests, 1)
        self.assertIn("限流", self.f.message)
        self.open_output_directory_dialog()
        self.lua.execute("pending_save(); test_clock = test_clock + 3599")
        self.f.http_code = 200
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.lua.execute("test_clock = test_clock + 1")
        self.refresh()
        self.assertEqual(self.f.requests, 2)

    def test_rate_limiting_survives_beijing_midnight(self):
        self.lua.globals().test_clock = int(datetime(2026, 11, 4, 15, 55, tzinfo=timezone.utc).timestamp())
        self.f.http_code = 429
        self.refresh()
        self.f.http_code = 200
        self.lua.execute("test_clock = test_clock + 600")
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.lua.execute("test_clock = test_clock + 3000")
        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertTrue((self.path / "20261105.png").exists())

    def test_rate_limit_already_received_survives_worker_timeout(self):
        self.f.http_code, self.f.hung = 429, True
        self.obj.refreshDaily(self.obj, True)
        self.assertEqual(self.f.pipe, "ERR\nhttp_429")
        self.f.now = 25
        self.f.drain(self.f)
        self.assertEqual(self.f.kills, 1)
        self.f.http_code, self.f.hung = 200, False
        self.lua.execute("test_clock = test_clock + 300")
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertIn("服务暂时限流", self.f.message)

    def test_rate_limit_already_received_survives_worker_cancellation(self):
        self.f.http_code, self.f.hung = 429, True
        self.obj.refreshDaily(self.obj, True)
        self.obj.stopPlugin(self.obj)
        self.f.drain(self.f)
        self.f.http_code, self.f.hung = 200, False
        self.lua.execute("plugin = loadPlugin(); obj = newPlugin(); test_clock = test_clock + 300")
        self.obj = self.lua.globals().obj
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertIn("服务暂时限流", self.f.message)

    def test_worker_start_failure_releases_unspent_attempt(self):
        settings = self.f.settings.data
        settings.yiyan_last_attempt_date = "20000101"
        self.f.spawn_failed = True
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.assertIn("可稍后重试", self.f.message)
        self.assertEqual(settings.yiyan_last_attempt_date, "20000101")
        self.assertFalse((self.path / (self.date + ".attempt")).exists())
        self.assertFalse(list(self.path.glob(".yiyan-work-*")))

        self.f.spawn_failed = False
        self.refresh()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_ambiguous_worker_start_error_keeps_attempt(self):
        self.f.spawn_hook_error = True
        self.f.http_error = True
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / (self.date + ".attempt")).exists())
        self.f.spawn_hook_error = self.f.http_error = False
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_valid_response_with_storage_failure_does_not_refetch(self):
        self.lua.execute(r'''
            local open = io.open
            io.open = function(path, mode)
                if path:match("/data%.partial$") and mode == "wb" then return nil, "disk full" end
                return open(path, mode)
            end
        ''')
        self.refresh()
        self.assertIn("无法保存", self.f.message)
        self.assertFalse((self.path / (self.date + ".json")).exists())
        self.lua.execute("test_clock = test_clock + 3600")
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_complete_partial_response_can_be_promoted_without_refetch(self):
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if src:match("/data%.partial$") and dst:match("/data$") then
                    return nil, "worker rename failed"
                end
                return rename(src, dst)
            end
        ''')
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.assertFalse(list(self.path.glob(".yiyan-work-*")))

    def test_received_sentence_survives_failed_cache_promotion_and_restart(self):
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if f.fail_cache and dst:match("%d+%.json$") then return nil, "disk full" end
                return rename(src, dst)
            end
        ''')
        cache_path = self.path / (self.date + ".json")
        self.f.fail_cache = True
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertIn("句子已保留", self.f.message)
        self.assertFalse(cache_path.exists())
        self.assertFalse(cache_path.with_suffix(".png").exists())
        stages = list(self.path.glob(".yiyan-work-*/data"))
        self.assertEqual(len(stages), 1)
        self.assertEqual(json.loads(stages[0].read_text())["payload"]["hitokoto"], self.payload["hitokoto"])

        self.lua.execute("plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        self.f.fail_cache, self.f.connected = False, False
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertEqual(json.loads(cache_path.read_text())["payload"]["hitokoto"], self.payload["hitokoto"])
        self.assertTrue(cache_path.with_suffix(".png").exists())
        self.assertFalse(list(self.path.glob(".yiyan-work-*")))

    def test_connecting_at_startup_requests_only_after_connection(self):
        self.f.connected = False
        self.obj.init(self.obj)
        self.lua.execute("table.remove(f.queue, 1)()")
        self.assertEqual(self.f.requests, 0)
        self.assertFalse((self.path / (self.date + ".attempt")).exists())
        self.f.connected = True
        self.obj.onNetworkConnected(self.obj)
        self.lua.execute("table.remove(f.queue, 1)(); table.remove(f.queue, 1)()")
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.obj.onCloseWidget(self.obj)
        self.f.drain(self.f)

    def test_failed_render_can_be_rebuilt_offline_without_refetch(self):
        self.f.fail_render = True
        self.refresh()
        self.assertTrue((self.path / (self.date + ".json")).exists())
        self.f.fail_render, self.f.connected = False, False
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_template_upgrade_uses_cached_sentence(self):
        self.cache(1)
        (self.path / (self.date + ".png")).write_bytes(PNG)
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.assertEqual(self.f.renders, 1)

    def test_body_keeps_closing_marks_with_preceding_punctuation(self):
        body = self.lua.globals().wallpaper.body
        for quote, expected in (
            ("她说：“明天见。”", "她说：“明天见。”"),
            ("怎么可能？！我不信。", "怎么可能？！\n我不信。"),
            ("「是真的！」她笑了。", "「是真的！」\n她笑了。"),
            ("你好，\r\n朋友。", "你好，\n朋友。"),
        ):
            with self.subTest(quote=quote):
                self.assertEqual(body(self.to_lua({"hitokoto": quote})), expected)

    def test_legacy_image_without_sentence_never_causes_another_request(self):
        (self.path / (self.date + ".png")).write_bytes(PNG)
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.assertEqual(self.f.renders, 0)

    def test_footer_has_white_glyphs_on_black_and_body_stays_above_stones(self):
        self.cache()
        self.refresh()
        body, source, date = [self.f.paints[i] for i in (1, 2, 3)]
        self.assertEqual((body.bg, body.fg), ("white", "black"))
        self.assertLess(body.y + body.h, 700)
        self.assertEqual((source.bg, source.fg), ("black", "white"))
        self.assertEqual(source.text, "一言开发者中心")
        self.assertTrue(source.bold)
        self.assertEqual(date.text, f"一言  ·  {self.date[:4]}-{self.date[4:6]}-{self.date[6:]}")
        self.assertGreaterEqual(source.y, 922)
        self.assertLessEqual(date.y + date.h, 1024)

    def test_oversized_source_is_kept_inside_footer(self):
        self.payload["from"] = "很长的出处" * 80
        self.cache()
        self.refresh()
        source = self.f.paints[2]
        self.assertLessEqual(source.h, 41)
        self.assertLess(source.y + source.h, self.f.paints[3].y)

    def test_disc_straddles_footer_and_title_centers_in_remaining_space(self):
        self.f.screen_width, self.f.screen_height, self.f.screen_scale = 1440, 1920, 2.4
        self.payload.update({"from": "一次长途旅行", "from_who": "作者"})
        self.cache()
        self.refresh()
        footer, disc = self.f.shapes[1], self.f.shapes[2]
        body, title, metadata = [self.f.paints[i] for i in (1, 2, 3)]
        self.assertEqual(disc.kind, "circle")
        self.assertEqual(disc.x - disc.radius, body.x)
        self.assertEqual(disc.y, footer.y)
        self.assertLess(disc.y - disc.radius, footer.y)
        self.assertLess(disc.y + disc.radius, 1920)
        self.assertEqual(title.x - (disc.x + disc.radius), 1440 - (title.x + title.w))
        self.assertEqual(title.alignment, "center")
        self.assertEqual((title.x, title.w, title.alignment), (metadata.x, metadata.w, metadata.alignment))
        self.assertEqual(title.text, "一次长途旅行")
        self.assertTrue(metadata.text.startswith("作者  ·  "))
        self.assertLess(title.y + title.h, metadata.y)
        self.assertLessEqual(metadata.y + metadata.h, 1920)
        self.assertLessEqual(title.size, 40)

    def test_missing_artwork_preserves_previous_png_and_cached_sentence(self):
        target = self.path / (self.date + ".png")
        target.write_bytes(PNG)
        self.cache(1)
        self.f.missing_background = True
        self.refresh()
        self.assertEqual(target.read_bytes(), PNG)
        self.assertEqual(self.f.requests, 0)

    def test_large_response_never_goes_through_the_status_pipe(self):
        payload = dict(self.payload, **{"from": "a" * 10000})
        self.f.raw_response = json.dumps(payload, ensure_ascii=False)
        self.refresh()
        self.assertEqual(self.f.pipe, "ok")
        self.assertEqual(self.f.requests, 1)

    def test_excessive_response_is_rejected_and_respects_cooldown(self):
        self.f.raw_response = "x" * 70000
        self.refresh()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertFalse((self.path / (self.date + ".png")).exists())
        self.f.raw_response = json.dumps(self.payload)
        self.lua.execute("test_clock = test_clock + 300")
        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_worker_timeout_is_reaped_and_releases_standby_lock(self):
        self.f.hung = True
        self.f.http_error = True
        self.obj.refreshDaily(self.obj, True)
        self.f.now = 25
        self.f.drain(self.f)
        self.assertEqual(self.f.kills, 1)
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.f.hung = self.f.http_error = False
        self.lua.execute("test_clock = test_clock + 300")
        self.refresh()
        self.assertEqual(self.f.requests, 2)

    def test_stop_cancels_worker_across_instances_and_preserves_daily_limit(self):
        old_archive = self.path / "20000101.png"
        old_archive.write_bytes(PNG)
        old_archive.with_suffix(".json").write_text(json.dumps({
            "date": "20000101", "owner": "yiyan.koplugin", "payload": self.payload,
            "image_digest": PNG.hex(),
        }))
        self.f.hung = True
        previous = self.obj
        previous.refreshDaily(previous, True)
        previous.onCloseWidget(previous)
        self.obj = self.lua.globals().newPlugin()
        self.obj.stopPlugin(self.obj)
        self.f.drain(self.f)
        self.assertEqual(self.f.kills, 1)
        self.assertFalse(previous._busy)
        self.assertFalse((self.path / (self.date + ".json")).exists())
        self.assertFalse((self.path / (self.date + ".png")).exists())
        self.assertFalse((self.path / ".yiyan-screen").exists())
        self.assertTrue(old_archive.exists())
        self.assertTrue(old_archive.with_suffix(".json").exists())
        self.assertFalse(list(self.path.glob(".yiyan-work-*")))
        self.obj = self.lua.globals().newPlugin()
        self.lua.execute("test_clock = test_clock + 3600")
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_widget_close_allows_already_started_request_to_finish(self):
        self.obj.refreshDaily(self.obj, True)
        self.obj.onCloseWidget(self.obj)
        self.f.drain(self.f)
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.assertIsNone(self.f.kills)

    def test_stop_discards_a_finished_response_before_it_can_be_published(self):
        self.obj.refreshDaily(self.obj, True)
        self.assertTrue(self.f.done)
        self.obj.stopPlugin(self.obj)
        self.f.drain(self.f)
        self.assertIsNone(self.f.kills)
        self.assertFalse((self.path / (self.date + ".json")).exists())
        self.assertFalse((self.path / ".yiyan-screen").exists())
        self.assertFalse(list(self.path.glob(".yiyan-work-*")))

    def test_stop_revokes_response_even_if_restart_drops_poll(self):
        self.f.hung = True
        self.obj.refreshDaily(self.obj, True)
        self.assertEqual(len(list(self.path.glob(".yiyan-work-*/data"))), 1)
        self.obj.stopPlugin(self.obj)
        self.assertEqual(self.f.kills, 1)
        self.assertEqual(len(self.f.queue), 1)
        marker = self.path / (self.date + ".attempt")
        self.assertEqual(marker.read_text(), self.date + "\nsuccess\n")

        self.lua.execute("f.queue = {}; f.locks = 0; plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertFalse(marker.with_suffix(".json").exists())
        self.assertFalse((self.path / ".yiyan-screen").exists())

    def test_stop_records_cancellation_if_attempt_marker_is_unwritable(self):
        self.f.hung = True
        self.obj.refreshDaily(self.obj, True)
        marker = self.path / (self.date + ".attempt")
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if dst:match("%.attempt$") then return nil, "unwritable" end
                return rename(src, dst)
            end
        ''')
        self.obj.stopPlugin(self.obj)
        self.assertIn(".yiyan-work-", marker.read_text())
        self.assertEqual(self.f.settings.data.yiyan_attempt_history[self.date], "cancelled_success")
        self.lua.execute("f.queue = {}; f.locks = 0; plugin = loadPlugin(); obj = newPlugin()")
        self.obj = self.lua.globals().obj
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertFalse(marker.with_suffix(".json").exists())

    def test_ui_transition_cancels_previous_periodic_callback(self):
        self.f.settings.data["yiyan_auto_screensaver"] = False
        self.obj.init(self.obj)
        old = self.obj
        self.obj = self.lua.globals().newPlugin()
        self.obj.init(self.obj)
        self.assertTrue(old._stopped)
        self.assertEqual(len(self.f.queue), 1)

    def test_cleanup_checks_ownership_and_leaves_replaced_images(self):
        self.cache()
        records = {
            "20000101": {},
            "20000102": {"owner": "yiyan.koplugin", "image_digest": PNG.hex()},
            "20000103": {"owner": "yiyan.koplugin", "image_digest": PNG.hex()},
        }
        for date, flags in records.items():
            (self.path / (date + ".json")).write_text(json.dumps({
                "date": date, "payload": self.payload, **flags,
            }))
            (self.path / (date + ".png")).write_bytes(PNG if date != "20000103" else b"user image")
        self.refresh()
        self.assertTrue((self.path / "20000101.png").exists())
        self.assertTrue((self.path / "20000101.json").exists())
        self.assertFalse((self.path / "20000102.png").exists())
        self.assertFalse((self.path / "20000102.json").exists())
        self.assertEqual((self.path / "20000103.png").read_bytes(), b"user image")
        self.assertTrue((self.path / "20000103.json").exists())
        display = self.path / ".yiyan-screen"
        self.assertEqual([p.name for p in display.iterdir()], ["wallpaper.png"])

    def test_cleanup_does_not_allow_replaced_archive_to_be_published_after_clock_rollback(self):
        self.refresh()
        replacement = (ROOT / "docs/demo.png").read_bytes()
        self.lua.globals().alternate_test_png = replacement
        archive = self.path / (self.date + ".png")
        archive.write_bytes(replacement)
        self.lua.execute("test_clock = test_clock + 86400")
        self.refresh()
        self.assertTrue(archive.with_suffix(".json").exists())
        self.lua.execute("test_clock = test_clock - 86400")
        self.refresh()
        self.assertIn("同名图片无法确认归属", self.f.message)
        self.assertEqual((self.path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)
        self.assertEqual(archive.read_bytes(), replacement)
        self.assertEqual(self.f.requests, 2)

    def test_cleanup_listing_failure_does_not_interrupt_publication(self):
        self.lua.execute(r'''
            package.loaded["libs/libkoreader-lfs"].dir = function() error("cannot open directory") end
        ''')
        self.refresh()
        self.assertEqual(self.f.settings.data.screensaver_type, "random_image")
        self.assertEqual((self.path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)
        self.assertIn("已生成", self.f.message)

    def test_encoder_error_or_truncation_preserves_previous_png(self):
        target = self.path / (self.date + ".png")
        target.write_bytes(PNG)
        self.cache(1)
        self.f.truncate_png = True
        for encoder_error in (True, False):
            with self.subTest(encoder_error=encoder_error):
                self.f.fail_encode = encoder_error
                self.refresh()
                self.assertEqual(target.read_bytes(), PNG)
                self.assertFalse(target.with_suffix(".png.tmp").exists())

    def test_offline_start_keeps_original_screensaver_until_image_is_ready(self):
        self.f.connected = False
        self.f.settings.data["screensaver_type"] = "cover"
        self.obj.init(self.obj)
        self.obj.refreshDaily(self.obj, True)
        self.assertEqual(self.f.settings.data["screensaver_type"], "cover")
        self.assertIsNone(self.f.settings.data["yiyan_screensaver_backup"])
        self.cache()
        self.obj.refreshDaily(self.obj, True)
        self.assertEqual(self.f.settings.data["screensaver_type"], "random_image")
        self.assertEqual(self.f.settings.data["screensaver_dir"], str(self.path / ".yiyan-screen") + "/")
        self.obj.stopPlugin(self.obj)
        self.assertEqual(self.f.settings.data["screensaver_type"], "cover")

    def test_landscape_reader_renders_portrait_wallpaper(self):
        self.cache(2)
        (self.path / (self.date + ".png")).write_bytes(PNG)
        self.f.screen_width, self.f.screen_height = 1024, 758
        self.refresh()
        self.assertEqual((self.f.export_width, self.f.export_height), (758, 1024))
        self.assertEqual(self.f.requests, 0)

    def open_output_directory_dialog(self):
        new_path = self.path / "new-output"
        self.lua.globals().test_next_dir = str(new_path)
        self.lua.execute(r'''
            local function map(path)
                local prefix = "/mnt/us/test/"
                if path == "/mnt/us/test" then return test_next_dir end
                if path:sub(1, #prefix) == prefix then
                    return test_next_dir .. "/" .. path:sub(#prefix + 1)
                end
                return path
            end
            local open, rename, remove = io.open, os.rename, os.remove
            io.open = function(path, ...) return open(map(path), ...) end
            os.rename = function(src, dst) return rename(map(src), map(dst)) end
            os.remove = function(path) return remove(map(path)) end
            local lfs = package.loaded["libs/libkoreader-lfs"]
            local attributes, symlinkattributes, mkdir, rmdir, list =
                lfs.attributes, lfs.symlinkattributes, lfs.mkdir, lfs.rmdir, lfs.dir
            lfs.attributes = function(path, ...) return attributes(map(path), ...) end
            lfs.symlinkattributes = function(path, ...) return symlinkattributes(map(path), ...) end
            lfs.mkdir = function(path) return mkdir(map(path)) end
            lfs.rmdir = function(path) return rmdir(map(path)) end
            lfs.dir = function(path) return list(map(path)) end
            package.loaded["util"].makePath = function(path)
                py_mkdir(map(path))
                return true
            end
            local ui = package.loaded["ui/uimanager"]
            ui.close = function() end
            package.loaded["ui/widget/inputdialog"].new = function(_, options)
                options.getInputText = function() return "/mnt/us/test/" end
                options.onShowKeyboard = function() end
                pending_save = options.buttons[1][2].callback
                return options
            end
            obj:chooseOutputDirectory()
        ''')
        return new_path

    def test_directory_save_waits_for_active_request(self):
        # The timer can start a request while the directory dialog is open.
        new_path = self.open_output_directory_dialog()
        self.lua.execute("obj:refreshDaily(true); pending_save()")
        self.assertIsNone(self.f.settings.data["yiyan_save_dir"])
        self.assertIn("请等待", self.f.message)
        self.f.drain(self.f)
        self.assertTrue((self.path / (self.date + ".png")).exists())
        self.assertTrue((self.path / (self.date + ".json")).exists())
        self.lua.execute("pending_save()")
        self.assertEqual(self.f.settings.data["yiyan_save_dir"], "/mnt/us/test/")
        for suffix in (".png", ".json", ".attempt"):
            name = self.date + suffix
            self.assertEqual((new_path / name).read_bytes(), (self.path / name).read_bytes())
        self.assertEqual((new_path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_directory_migration_reports_file_conflict(self):
        self.refresh()
        dest = self.open_output_directory_dialog()
        dest.mkdir()
        archive = dest / (self.date + ".png")
        archive.write_bytes(b"other data")
        self.lua.execute("pending_save()")
        self.assertIn("新目录已有不同的今日壁纸数据", self.f.message)
        self.assertIsNone(self.f.settings.data.yiyan_save_dir)
        self.assertEqual(archive.read_bytes(), b"other data")

    def test_directory_migration_reports_and_rolls_back_write_failure(self):
        self.refresh()
        dest = self.open_output_directory_dialog()
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if dst:match("^/mnt/us/test/%d+%.json$") then return nil, "disk full" end
                return rename(src, dst)
            end
            pending_save()
        ''')
        self.assertIn("无法复制今日壁纸数据", self.f.message)
        self.assertIsNone(self.f.settings.data.yiyan_save_dir)
        self.assertFalse(list(dest.iterdir()))
        self.assertTrue((self.path / (self.date + ".png")).exists())

    def test_directory_migration_rejects_foreign_cache_before_publication(self):
        self.f.connected = False
        dest = self.open_output_directory_dialog()
        dest.mkdir()
        archive = dest / (self.date + ".png")
        archive.write_bytes(PNG)
        foreign_cache = archive.with_suffix(".json")
        foreign_cache.write_text('{"owner":"another application"}')
        self.lua.execute("pending_save()")
        self.assertIn("同名 JSON", self.f.message)
        self.assertIsNone(self.f.settings.data.yiyan_save_dir)
        self.assertIsNone(self.f.settings.data.screensaver_type)
        self.assertFalse((dest / ".yiyan-screen").exists())
        self.assertEqual(archive.read_bytes(), PNG)
        self.assertEqual(foreign_cache.read_text(), '{"owner":"another application"}')

    def test_directory_migration_leaves_foreign_source_data_in_place(self):
        archive = self.path / (self.date + ".png")
        archive.write_bytes(PNG)
        cache = archive.with_suffix(".json")
        cache.write_text('{"owner":"another application"}')
        marker = archive.with_suffix(".attempt")
        marker.write_text("unrelated data")
        dest = self.open_output_directory_dialog()
        self.lua.execute("pending_save()")
        self.assertEqual(self.f.settings.data.yiyan_save_dir, "/mnt/us/test/")
        self.assertEqual(list(dest.iterdir()), [])
        self.assertEqual(archive.read_bytes(), PNG)
        self.assertEqual(cache.read_text(), '{"owner":"another application"}')
        self.assertEqual(marker.read_text(), "unrelated data")
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((dest / archive.name).exists())

    def test_directory_migration_listing_failure_leaves_destination_untouched(self):
        self.refresh()
        dest = self.open_output_directory_dialog()
        self.lua.execute(r'''
            local lfs = package.loaded["libs/libkoreader-lfs"]
            local list = lfs.dir
            lfs.dir = function(path)
                if path == test_dir .. "/" then error("cannot open directory") end
                return list(path)
            end
            pending_save()
        ''')
        self.assertIn("无法读取原目录的请求记录", self.f.message)
        self.assertIsNone(self.f.settings.data.yiyan_save_dir)
        self.assertEqual(list(dest.iterdir()), [])
        self.assertEqual((self.path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)

    def test_directory_migration_and_clock_rollback_keep_daily_attempt_history(self):
        self.refresh()
        self.lua.execute("test_clock = test_clock + 86400")
        self.refresh()
        settings = self.f.settings.data
        self.assertTrue(settings.yiyan_attempt_history["20261104"])
        self.assertTrue(settings.yiyan_attempt_history["20261105"])
        # The success markers independently survive resetting the settings.
        settings.yiyan_attempt_history = None
        self.open_output_directory_dialog()
        self.lua.execute("pending_save()")
        self.assertEqual(settings.yiyan_save_dir, "/mnt/us/test/")
        self.assertTrue(settings.yiyan_attempt_history["20261104"])
        self.assertTrue(settings.yiyan_attempt_history["20261105"])

        self.lua.execute("test_clock = test_clock - 86400")
        self.refresh()
        self.assertEqual(self.f.requests, 2)
        self.assertIn("今日已成功获取", self.f.message)

    def test_directory_migration_preserves_a_sentence_awaiting_cache_promotion(self):
        new_path = self.open_output_directory_dialog()
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if dst:sub(1, #test_dir) == test_dir and dst:match("%d+%.json$") then
                    return nil, "unwritable old cache"
                end
                return rename(src, dst)
            end
        ''')
        self.refresh()
        self.assertFalse((self.path / (self.date + ".json")).exists())
        self.lua.execute("pending_save()")
        self.assertEqual(self.f.settings.data["yiyan_save_dir"], "/mnt/us/test/")
        self.assertEqual((new_path / (self.date + ".attempt")).read_text(), self.date + "\nsuccess\n")
        self.f.connected = False
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertEqual((new_path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)

    def test_directory_migration_preserves_a_different_staged_destination_sentence(self):
        self.refresh()
        dest = self.open_output_directory_dialog()
        stage = dest / ".yiyan-work-123-1"
        stage.mkdir(parents=True)
        marker = dest / (self.date + ".attempt")
        marker.write_text(self.date + "\n" + stage.name + "\n")
        staged_quote = {"date": self.date, "owner": "yiyan.koplugin",
                        "payload": {"hitokoto": "目录里已有另一句"}}
        staged_path = stage / "data"
        staged_path.write_text(json.dumps(staged_quote))
        self.lua.execute("pending_save()")
        self.assertIn("新目录已有不同的今日壁纸数据", self.f.message)
        self.assertIsNone(self.f.settings.data.yiyan_save_dir)
        self.assertEqual(json.loads(staged_path.read_text()), staged_quote)
        self.assertFalse(marker.with_suffix(".json").exists())

    def test_foreign_cache_is_preserved_before_and_during_request(self):
        path = self.path / (self.date + ".json")
        original = b'{"application":"another plugin","data":"personal notes"}'
        path.write_bytes(original)
        self.refresh()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.f.requests, 0)
        self.assertFalse(path.with_suffix(".attempt").exists())

        path.unlink()
        self.obj.refreshDaily(self.obj, True)
        path.write_bytes(original)
        self.f.drain(self.f)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.f.requests, 1)
        self.assertFalse(path.with_suffix(".png").exists())
        path.unlink()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue(path.with_suffix(".png").exists())

    def test_rebuild_does_not_overwrite_user_replacement(self):
        self.refresh()
        path = self.path / (self.date + ".png")
        path.write_bytes(b"user replacement")
        self.refresh()
        self.assertEqual(path.read_bytes(), b"user replacement")
        self.assertEqual(self.f.requests, 1)

    def test_normal_refresh_does_not_publish_valid_user_replacement(self):
        self.refresh()
        archive = self.path / (self.date + ".png")
        display = self.path / ".yiyan-screen/wallpaper.png"
        replacement = (ROOT / "docs/demo.png").read_bytes()
        self.lua.globals().alternate_test_png = replacement
        archive.write_bytes(replacement)
        self.refresh()
        self.assertIn("同名图片无法确认归属", self.f.message)
        self.assertEqual(display.read_bytes(), PNG)
        self.assertEqual(archive.read_bytes(), replacement)
        self.assertEqual(self.f.requests, 1)
        new_path = self.open_output_directory_dialog()
        self.lua.execute("pending_save()")
        self.assertEqual((new_path / ".yiyan-screen/wallpaper.png").read_bytes(), PNG)
        self.assertFalse((new_path / archive.name).exists())
        self.refresh()
        self.assertEqual((new_path / archive.name).read_bytes(), PNG)
        self.assertEqual(archive.read_bytes(), replacement)
        self.assertEqual(self.f.requests, 1)

    def test_refresh_preserves_foreign_temporary_files_and_directories(self):
        self.lua.globals().test_time = int(datetime(2026, 11, 4, 8, tzinfo=timezone.utc).timestamp())
        self.lua.execute("os.time = function() return test_time end")
        display_dir = self.path / ".yiyan-screen"
        display_dir.mkdir()
        foreign_stage = self.path / f".yiyan-work-{self.lua.globals().test_time}-1"
        foreign_stage.mkdir()
        foreign_files = [
            self.path / ("20261104" + suffix)
            for suffix in (".response.tmp", ".response.tmp.tmp", ".attempt.tmp", ".json.tmp", ".png.tmp")
        ] + [display_dir / "wallpaper.png.tmp", foreign_stage / "data"]
        for file in foreign_files:
            file.write_bytes(b"unrelated data")
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertEqual((display_dir / "wallpaper.png").read_bytes(), PNG)
        for file in foreign_files:
            self.assertEqual(file.read_bytes(), b"unrelated data")
        self.assertEqual(list(self.path.glob(".yiyan-work-*")), [foreign_stage])
        self.assertFalse(list(display_dir.glob(".yiyan-work-*")))

    def test_render_transaction_recovers_at_each_rename_boundary(self):
        self.refresh()
        archive = self.path / (self.date + ".png")
        cache = archive.with_suffix(".json")
        display = self.path / ".yiyan-screen/wallpaper.png"
        old_cache = json.loads(cache.read_text())
        old_cache["layout_version"] = 5
        new_png = (ROOT / "docs/demo.png").read_bytes()
        self.lua.globals().alternate_test_png = new_png
        self.f.invalid_png = new_png
        self.lua.execute(r'''
            local rename = os.rename
            os.rename = function(src, dst)
                if dst:match("%d+%.json$") then
                    f.cache_renames = f.cache_renames + 1
                    if f.cache_renames == f.fail_cache_rename then return nil, "disk full" end
                end
                if f.fail_archive_rename and dst:match("%d+%.png$") then return nil, "disk full" end
                return rename(src, dst)
            end
        ''')
        for failure in ("pending_cache", "archive", "final_cache"):
            with self.subTest(failure=failure):
                archive.write_bytes(PNG)
                display.write_bytes(PNG)
                cache.write_text(json.dumps(old_cache))
                self.f.cache_renames = 0
                self.f.fail_cache_rename = {"pending_cache": 1, "final_cache": 2}.get(failure)
                self.f.fail_archive_rename = failure == "archive"
                self.refresh()
                self.assertEqual(archive.read_bytes(), new_png if failure == "final_cache" else PNG)
                self.assertEqual(display.read_bytes(), PNG)
                interrupted = json.loads(cache.read_text())
                self.assertEqual(interrupted["image_digest"], PNG.hex())
                self.assertEqual("pending_image" in interrupted, failure != "pending_cache")
                self.assertFalse(list(self.path.glob(".yiyan-work-*")))

                self.f.fail_cache_rename, self.f.fail_archive_rename = None, False
                self.f.connected = False
                self.obj = self.lua.globals().newPlugin()
                previous_renders = self.f.renders
                self.refresh()
                self.assertEqual(archive.read_bytes(), new_png)
                self.assertEqual(display.read_bytes(), new_png)
                recovered = json.loads(cache.read_text())
                self.assertEqual(recovered["layout_version"], 7)
                self.assertEqual(recovered["image_digest"], new_png.hex())
                self.assertNotIn("pending_image", recovered)
                self.assertEqual(self.f.renders - previous_renders, 0 if failure == "final_cache" else 1)
                self.assertEqual(self.f.requests, 1)

    def test_undecodable_png_with_valid_header_and_tail_is_never_published(self):
        display = self.path / ".yiyan-screen/wallpaper.png"
        display.parent.mkdir()
        display.write_bytes(PNG)
        archive = self.path / (self.date + ".png")
        archive.write_bytes(BROKEN_PNG)
        self.refresh()
        self.assertEqual(display.read_bytes(), PNG)
        self.assertEqual(archive.read_bytes(), BROKEN_PNG)
        self.assertEqual(self.f.requests, 0)

        archive.write_bytes(PNG)
        self.cache(2)
        self.f.invalid_png = BROKEN_PNG
        self.refresh()
        self.assertEqual(archive.read_bytes(), PNG)
        self.assertEqual(display.read_bytes(), PNG)

    def test_periodic_refresh_and_disable_respect_manual_screensaver_change(self):
        settings = self.f.settings.data
        settings["screensaver_type"] = "cover"
        self.refresh()
        self.assertEqual(settings["screensaver_type"], "random_image")
        settings["screensaver_type"] = "readingprogress"
        settings["screensaver_dir"] = "/mnt/us/my-wallpapers/"
        self.refresh()
        self.obj.stopPlugin(self.obj)
        self.assertEqual(settings["screensaver_type"], "readingprogress")
        self.assertEqual(settings["screensaver_dir"], "/mnt/us/my-wallpapers/")
        self.assertIsNone(settings["yiyan_screensaver_backup"])

    def test_disabling_restores_only_screensaver_settings_the_plugin_still_owns(self):
        settings = self.f.settings.data
        settings.screensaver_type = "cover"
        settings.screensaver_dir = "/mnt/us/original-wallpapers/"
        settings.screensaver_stretch_images = False
        settings.screensaver_show_message = True
        self.refresh()
        settings.screensaver_type = "readingprogress"
        self.obj.stopPlugin(self.obj)
        self.assertEqual(settings.screensaver_type, "readingprogress")
        self.assertEqual(settings.screensaver_dir, "/mnt/us/original-wallpapers/")
        self.assertFalse(settings.screensaver_stretch_images)
        self.assertTrue(settings.screensaver_show_message)
        self.assertIsNone(settings.yiyan_screensaver_backup)

    def test_disabling_keeps_random_mode_for_manually_selected_folder(self):
        settings = self.f.settings.data
        settings.screensaver_type = "cover"
        settings.screensaver_show_message = True
        self.refresh()
        settings.screensaver_dir = "/mnt/us/my-wallpapers/"
        menu = self.lua.table()
        self.obj.addToMainMenu(self.obj, menu)
        menu.yiyan.sub_item_table[1].callback()
        self.assertEqual(settings.screensaver_type, "random_image")
        self.assertEqual(settings.screensaver_dir, "/mnt/us/my-wallpapers/")
        self.assertTrue(settings.screensaver_show_message)
        self.assertIsNone(settings.yiyan_screensaver_backup)

    def test_screensaver_backup_and_first_apply_are_saved_together(self):
        self.cache()
        self.lua.execute(r'''
            function f.settings:flush()
                if self.data.yiyan_screensaver_backup == nil and self.data.screensaver_type == nil then return end
                f.flushes = (f.flushes or 0) + 1
                assert(self.data.yiyan_screensaver_backup)
                assert(self.data.screensaver_type == "random_image")
                assert(self.data.screensaver_dir == self.data.yiyan_screensaver_backup.applied_dir)
                assert(self.data.screensaver_stretch_images)
                assert(self.data.screensaver_show_message == false)
            end
        ''')
        self.refresh()
        self.assertEqual(self.f.flushes, 1)

    def test_incomplete_legacy_screensaver_apply_is_recovered(self):
        settings = self.f.settings.data
        settings.screensaver_type = "cover"
        settings.screensaver_dir = "/mnt/us/original-wallpapers/"
        settings.screensaver_show_message = True
        display_dir = str(self.path / ".yiyan-screen") + "/"
        settings.yiyan_screensaver_backup = self.to_lua({
            "version": 1, "applied_dir": display_dir,
            "type": {"is_set": True, "value": settings.screensaver_type},
            "dir": {"is_set": True, "value": settings.screensaver_dir},
            "stretch": {"is_set": False},
            "show_message": {"is_set": True, "value": True},
        })
        self.cache()
        self.refresh()
        self.assertEqual(settings.screensaver_type, "random_image")
        self.assertEqual(settings.screensaver_dir, display_dir)
        self.assertEqual(settings.yiyan_screensaver_backup.version, 2)
        self.obj.stopPlugin(self.obj)
        self.assertEqual(settings.screensaver_type, "cover")
        self.assertEqual(settings.screensaver_dir, "/mnt/us/original-wallpapers/")
        self.assertIsNone(settings.screensaver_stretch_images)
        self.assertTrue(settings.screensaver_show_message)

    def test_request_rechecks_beijing_date_inside_worker(self):
        self.lua.globals().review_clock = int(datetime(2026, 11, 4, 15, 59, 59, tzinfo=timezone.utc).timestamp())
        self.lua.execute(r'''
            os.time = function() return review_clock end
            local ffi = package.loaded["ffi/util"]
            local spawn = ffi.runInSubProcess
            ffi.runInSubProcess = function(...)
                review_clock = review_clock + 1
                return spawn(...)
            end
        ''')
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / "20261105.png").exists())
        self.refresh()
        self.assertEqual(self.f.requests, 1)

    def test_connection_crossing_beijing_midnight_never_sends_old_day_request(self):
        self.lua.globals().test_clock = int(datetime(2026, 11, 4, 15, 59, 35, tzinfo=timezone.utc).timestamp())
        self.lua.execute(r'''
            local http = package.loaded["socket.http"]
            local open = http.open
            http.open = function(...)
                test_clock = test_clock + 26
                return open(...)
            end
        ''')
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.assertEqual(self.f.closed_connections, 1)
        self.assertFalse((self.path / "20261104.attempt").exists())
        self.refresh()
        self.refresh()
        self.assertEqual(self.f.requests, 1)
        self.assertTrue((self.path / "20261105.png").exists())

    def test_request_is_deferred_inside_the_midnight_send_window(self):
        self.lua.globals().test_clock = int(datetime(2026, 11, 4, 15, 59, 45, tzinfo=timezone.utc).timestamp())
        self.refresh()
        self.assertEqual(self.f.requests, 0)
        self.assertIsNone(self.f.settings.data.yiyan_attempt_history)
        self.assertFalse((self.path / (self.date + ".attempt")).exists())
        self.lua.execute("test_clock = test_clock + 20")
        self.refresh()
        self.assertEqual(self.f.requests, 1)


if __name__ == "__main__":
    unittest.main()
