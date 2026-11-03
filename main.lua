-- Yiyan daily wallpaper for KOReader / Kindle.
-- Network I/O runs in a child process so it never blocks the reader UI.

local WidgetContainer = require("ui/widget/container/widgetcontainer")
local UIManager = require("ui/uimanager")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local Device = require("device")
local util = require("util")
local ffiutil = require("ffi/util")
local http = require("socket.http")
local socketutil = require("socketutil")
local NetworkMgr = require("ui/network/manager")
local logger = require("logger")
local json = require("json")
local time = require("ui/time")
local _ = require("gettext")
local plugin_dir = debug.getinfo(1, "S").source:match("^@(.*/)") or "plugins/yiyan.koplugin/"
local Wallpaper = dofile(plugin_dir .. "wallpaper.lua")

local Yiyan = WidgetContainer:extend{ name = "yiyan" }

local SAVE_DIR = "/mnt/us/koreader/screenshots/yiyan/"
local STARTUP_DELAY = 8
local CHECK_INTERVAL = 5 * 60
local HTTP_TIMEOUT = 20
local POLL_INTERVAL = 0.125
local UTC_OFFSET = 8 * 3600 -- 一言壁纸统一按北京时间归档

local SETTING_AUTO = "yiyan_auto_screensaver"
local SETTING_BACKUP = "yiyan_screensaver_backup"
local SETTING_LAST_ATTEMPT = "yiyan_last_attempt_date"
local SETTING_SAVE_DIR = "yiyan_save_dir"

local active_worker
local active_instance
local function workerNow() return time.to_s(time.boottime_or_monotonic()) end
local function todayInfo()
    local t = os.date("!*t", os.time() + UTC_OFFSET)
    local date = string.format("%04d%02d%02d", t.year, t.month, t.day)
    return date, SAVE_DIR .. date .. ".png"
end

local function fileExists(path)
    local f = io.open(path, "rb")
    if not f then return false end
    local size = f:seek("end")
    f:close()
    return size and size > 0
end

local function readFile(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local data = f:read("*a")
    f:close()
    return data
end

local function writeAtomic(path, data)
    local tmp = path .. ".tmp"
    local f = io.open(tmp, "wb")
    if not f then return false end
    local written = f:write(data)
    local closed = f:close()
    if not written or not closed or not os.rename(tmp, path) then
        os.remove(tmp)
        return false
    end
    return true
end

local function validPNG(path)
    local f = io.open(path, "rb")
    if not f then return false end
    local head = f:read(8)
    local size = f:seek("end")
    local tail
    if size and size >= 32 then f:seek("end", -12); tail = f:read(12) end
    f:close()
    return head == "\137PNG\r\n\26\n" and tail == "\0\0\0\0IEND\174\66\96\130"
end

local function validPayload(payload)
    return type(payload) == "table" and type(payload.hitokoto) == "string"
        and payload.hitokoto:match("%S") ~= nil and #payload.hitokoto <= 8192
end

local function loadCache(date)
    local raw = readFile(SAVE_DIR .. date .. ".json")
    if not raw then return nil end
    local ok, cached = pcall(json.decode, raw)
    if ok and type(cached) == "table" and cached.date == date and validPayload(cached.payload) then
        return cached
    end
end

local function saveCache(cached)
    local ok, raw = pcall(json.encode, cached)
    return ok and type(raw) == "string" and writeAtomic(SAVE_DIR .. cached.date .. ".json", raw)
end

local function networkIsConnected()
    if NetworkMgr and NetworkMgr.isConnected then
        local ok, connected = pcall(NetworkMgr.isConnected, NetworkMgr)
        if ok then return connected == true end
    end
    return false
end

local function autoScreensaverEnabled()
    local enabled = G_reader_settings:readSetting(SETTING_AUTO)
    return enabled == nil or enabled == true
end

local function setAutoScreensaver(enabled)
    G_reader_settings:saveSetting(SETTING_AUTO, enabled)
    G_reader_settings:flush()
end

local function configureScreensaver()
    local backup = G_reader_settings:readSetting(SETTING_BACKUP)
    if type(backup) ~= "table" or backup.version ~= 1 then
        local function snapshot(key)
            local value = G_reader_settings:readSetting(key)
            return { is_set = value ~= nil, value = value }
        end
        backup = {
            version = 1,
            type = snapshot("screensaver_type"),
            dir = snapshot("screensaver_dir"),
            stretch = snapshot("screensaver_stretch_images"),
            show_message = snapshot("screensaver_show_message"),
        }
        G_reader_settings:saveSetting(SETTING_BACKUP, backup)
        G_reader_settings:flush()
    end
    G_reader_settings:saveSetting("screensaver_type", "random_image")
    G_reader_settings:saveSetting("screensaver_dir", SAVE_DIR)
    G_reader_settings:saveSetting("screensaver_stretch_images", true)
    G_reader_settings:saveSetting("screensaver_show_message", false)
    G_reader_settings:flush()
end

local function restoreScreensaver(disable_auto)
    local backup = G_reader_settings:readSetting(SETTING_BACKUP)
    if type(backup) == "table" and backup.version == 1 then
        local function restore(key, saved)
            if type(saved) ~= "table" or not saved.is_set then
                G_reader_settings:delSetting(key)
            else
                G_reader_settings:saveSetting(key, saved.value)
            end
        end
        restore("screensaver_type", backup.type)
        restore("screensaver_dir", backup.dir)
        restore("screensaver_stretch_images", backup.stretch)
        restore("screensaver_show_message", backup.show_message)
    end
    if disable_auto then G_reader_settings:saveSetting(SETTING_AUTO, false) end
    G_reader_settings:flush()
    if backup ~= nil then
        G_reader_settings:delSetting(SETTING_BACKUP)
        G_reader_settings:flush()
    end
end

function Yiyan:init()
    if active_instance then active_instance:onCloseWidget() end
    active_instance = self
    self.ui.menu:registerToMainMenu(self)
    self._stopped = false
    self._busy = false
    local configured_dir = G_reader_settings:readSetting(SETTING_SAVE_DIR)
    if type(configured_dir) == "string" and configured_dir:match("^/mnt/us/") then
        SAVE_DIR = configured_dir:gsub("/*$", "") .. "/"
    end
    util.makePath(SAVE_DIR)
    if autoScreensaverEnabled() then configureScreensaver() else restoreScreensaver(false) end
    self:_schedule(STARTUP_DELAY)
end

function Yiyan:_schedule(delay)
    if self._stopped then return end
    if self._check_callback then UIManager:unschedule(self._check_callback) end
    self._check_callback = function()
        self._check_callback = nil
        if self._stopped then return end
        self:refreshDaily(false)
        self:_schedule(CHECK_INTERVAL)
    end
    UIManager:scheduleIn(delay, self._check_callback)
end

function Yiyan:onResume()
    if not self._stopped then self:_schedule(2) end
end

function Yiyan:onCloseWidget()
    self._stopped = true
    if self._check_callback then UIManager:unschedule(self._check_callback); self._check_callback = nil end
    if active_instance == self then active_instance = nil end
end

function Yiyan:stopPlugin()
    self:onCloseWidget()
    restoreScreensaver(false)
end

function Yiyan:requestSentence(date, callback)
    if active_worker then
        callback(nil, "busy")
        return
    end
    local response_path = SAVE_DIR .. date .. ".response.tmp"
    os.remove(response_path)
    local pid, read_fd
    local started, start_err = pcall(function()
        pid, read_fd = ffiutil.runInSubProcess(function(_, child_write_fd)
            local result
            local ok = pcall(function()
                socketutil:set_timeout(HTTP_TIMEOUT, HTTP_TIMEOUT)
                local chunks, response_size = {}, 0
                local request_ok, code = http.request{
                    url = "https://v1.hitokoto.cn/?encode=json",
                    method = "GET",
                    headers = { ["User-Agent"] = "KOReader-yiyan.koplugin/1.1" },
                    redirect = false,
                    sink = function(chunk, sink_err)
                        if sink_err then return nil, sink_err end
                        if chunk then
                            response_size = response_size + #chunk
                            if response_size > 65536 then return nil, "response_too_large" end
                            chunks[#chunks + 1] = chunk
                        end
                        return 1
                    end,
                }
                if not request_ok then
                    result = "ERR\nnetwork_error"
                elseif tonumber(code) ~= 200 then
                    result = "ERR\nhttp_" .. tostring(code or "unknown")
                else
                    -- Only send a small status through the pipe. The parent
                    -- waits for exit before reading it; a full JSON pipe could
                    -- otherwise block the child before it can exit.
                    result = writeAtomic(response_path, table.concat(chunks)) and "ok" or "write_error"
                end
            end)
            if not ok then result = "ERR\nnetwork_error" end
            ffiutil.writeToFD(child_write_fd, result or "ERR\nnetwork_error", true)
        end, true)
    end)
    if not started or not pid then
        logger.warn("Yiyan: cannot start request worker", tostring(start_err or read_fd))
        callback(nil, "network_error")
        return
    end

    active_worker = { pid = pid, read_fd = read_fd, callback = callback,
        deadline = workerNow() + HTTP_TIMEOUT, timed_out = false }
    UIManager:preventStandby()

    local function poll()
        local worker = active_worker
        if not worker or worker.pid ~= pid then return end
        if not ffiutil.isSubProcessDone(pid) then
            if workerNow() >= worker.deadline or worker.timed_out then
                worker.timed_out = true
                -- A kill can race the child's initial process-group setup.
                -- Retry until waitpid confirms the old writer has exited.
                ffiutil.terminateSubProcess(pid)
            end
            UIManager:scheduleIn(POLL_INTERVAL, poll)
            return
        end

        active_worker = nil
        UIManager:allowStandby()
        local read_ok, response = pcall(ffiutil.readAllFromFD, worker.read_fd)
        local raw = readFile(response_path)
        os.remove(response_path)
        os.remove(response_path .. ".tmp")
        if worker.timed_out then
            worker.callback(nil, "timeout")
        elseif not read_ok or type(response) ~= "string" then
            worker.callback(nil, "network_error")
        elseif response == "ok" and raw then
            local decoded_ok, payload = pcall(json.decode, raw)
            if decoded_ok and validPayload(payload) then
                worker.callback(payload)
            else
                worker.callback(nil, "invalid_response")
            end
        else
            worker.callback(nil, response:match("^ERR\n(.*)") or "network_error")
        end
    end
    UIManager:scheduleIn(POLL_INTERVAL, poll)
end

function Yiyan:renderImage(payload, date, target)
    return Wallpaper.render(self.path or plugin_dir:gsub("/$", ""), payload, date, target,
        Device.screen:getWidth(), Device.screen:getHeight())
end

function Yiyan:_renderCache(cached, image_path, show_message)
    local ok = self:renderImage(cached.payload, cached.date, image_path)
    if not ok or not validPNG(image_path) then
        if show_message and not self._stopped then
            UIManager:show(InfoMessage:new{ text = _("图片生成失败，句子已缓存，可稍后离线重新生成") })
        end
        return false
    end
    cached.layout_version = Wallpaper.VERSION
    saveCache(cached)
    self:cleanupOldImages(image_path)
    if show_message and not self._stopped then
        UIManager:show(InfoMessage:new{ text = _("今日一言壁纸已生成") })
    end
    return true
end

function Yiyan:cleanupOldImages(keep_path)
    local ok, lfs = pcall(require, "libs/libkoreader-lfs")
    if not ok then return end
    for name in lfs.dir(SAVE_DIR) do
        if name:match("^%d%d%d%d%d%d%d%d%.png$") or name:match("^%d%d%d%d%d%d%d%d%.json$") then
            local path = SAVE_DIR .. name
            local keep_stem = keep_path:match("(%d%d%d%d%d%d%d%d)%.png$")
            if name:sub(1, 8) ~= keep_stem and lfs.attributes(path, "mode") == "file" then os.remove(path) end
        end
    end
end

function Yiyan:refreshDaily(show_message, force_render, cache_only)
    if self._stopped then return end
    local function notify(message)
        if show_message and not self._stopped then UIManager:show(InfoMessage:new{ text = message }) end
    end
    if self._busy or active_worker then notify(_("一言任务正在进行中")); return end
    util.makePath(SAVE_DIR)
    local date, image_path = todayInfo()
    local cached = loadCache(date)
    if validPNG(image_path) and not force_render
        and (not cached or cached.layout_version == Wallpaper.VERSION) then
        notify(_("今日一言壁纸已存在"))
        return
    end
    -- Rebuilding from a cached sentence never opens a network connection, even
    -- if today's image is missing or the template changed since it was saved.
    if cached then self:_renderCache(cached, image_path, show_message); return end
    if validPNG(image_path) then
        notify(_("今日图片来自旧版，未缓存句子；保留原图，明天自动使用新模板"))
        return
    end
    if cache_only then notify(_("暂无今日句子缓存")); return end
    if G_reader_settings:readSetting(SETTING_LAST_ATTEMPT) == date
        or fileExists(SAVE_DIR .. date .. ".attempt") then
        notify(_("今天已请求过一言，明天再试"))
        return
    end
    if not networkIsConnected() then
        notify(_("网络未连接，今日请求尚未发出"))
        return
    end

    -- Persist the attempt before starting the child. If storage is unwritable,
    -- do not send a request that the next startup might accidentally repeat.
    if not writeAtomic(SAVE_DIR .. date .. ".attempt", date .. "\n") then
        notify(_("无法保存请求记录，请检查存储空间和目录权限"))
        return
    end
    G_reader_settings:saveSetting(SETTING_LAST_ATTEMPT, date)
    G_reader_settings:flush()
    self._busy = true
    self:requestSentence(date, function(payload, err)
        self._busy = false
        if not payload then
            logger.warn("Yiyan: daily request failed", tostring(err))
            notify(_("今日一言请求失败；为避免重复请求，明天再试"))
            return
        end
        local record = { date = date, payload = { hitokoto = payload.hitokoto,
            from = type(payload.from) == "string" and payload.from or "",
            from_who = type(payload.from_who) == "string" and payload.from_who or "",
            uuid = type(payload.uuid) == "string" and payload.uuid or nil } }
        if not saveCache(record) then
            notify(_("句子已返回，但缓存保存失败；请检查存储空间"))
            return
        end
        local current_date = todayInfo()
        if current_date == date then self:_renderCache(record, image_path, show_message) end
    end)
end

function Yiyan:chooseOutputDirectory()
    if self._busy or active_worker then
        UIManager:show(InfoMessage:new{ text = _("请等待当前任务完成后再修改目录") })
        return
    end
    local dialog
    dialog = InputDialog:new{
        title = _("屏保图片目录"),
        input = SAVE_DIR,
        input_hint = _("请输入 Kindle 上的绝对目录路径"),
        buttons = {{
            { text = _("取消"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("保存"), callback = function()
                local path = dialog:getInputText()
                if path and path:match("^/mnt/us/") then
                    local date, old_image = todayInfo()
                    SAVE_DIR = path:gsub("/*$", "") .. "/"
                    G_reader_settings:saveSetting(SETTING_SAVE_DIR, SAVE_DIR)
                    G_reader_settings:flush()
                    util.makePath(SAVE_DIR)
                    local new_image = SAVE_DIR .. date .. ".png"
                    if old_image ~= new_image and fileExists(old_image) and not fileExists(new_image) then
                        ffiutil.copyFile(old_image, new_image)
                    end
                    local old_dir = old_image:match("^(.*)/[^/]+$") .. "/"
                    if old_dir ~= SAVE_DIR then
                        for _, suffix in ipairs({ ".json", ".attempt" }) do
                            local data = readFile(old_dir .. date .. suffix)
                            if data then writeAtomic(SAVE_DIR .. date .. suffix, data) end
                        end
                    end
                    if autoScreensaverEnabled() then configureScreensaver() end
                end
                UIManager:close(dialog)
            end },
        }},
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function Yiyan:addToMainMenu(menu_items)
    menu_items.yiyan = {
        text = _("一言壁纸"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text = _("自动配置 KOReader 屏保"),
                checked_func = autoScreensaverEnabled,
                callback = function()
                    if autoScreensaverEnabled() then
                        restoreScreensaver(true)
                        UIManager:show(InfoMessage:new{ text = _("已关闭自动配置，原屏保设置已恢复") })
                    else
                        configureScreensaver()
                        setAutoScreensaver(true)
                        UIManager:show(InfoMessage:new{ text = _("已启用自动配置 KOReader 屏保") })
                    end
                end,
            },
            { text = _("生成今日壁纸"), callback = function() self:refreshDaily(true) end },
            { text = _("重新生成今日图片（不联网）"), callback = function() self:refreshDaily(true, true, true) end },
            { text = _("设置屏保图片目录"), callback = function() self:chooseOutputDirectory() end },
        },
    }
end

return Yiyan
