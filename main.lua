-- Yiyan daily wallpaper for KOReader / Kindle.
-- Network I/O runs in a child process so it never blocks the reader UI.

local WidgetContainer = require("ui/widget/container/widgetcontainer")
local UIManager = require("ui/uimanager")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local TextBoxWidget = require("ui/widget/textboxwidget")
local Font = require("ui/font")
local Blitbuffer = require("ffi/blitbuffer")
local Device = require("device")
local util = require("util")
local ffiutil = require("ffi/util")
local http = require("socket.http")
local socketutil = require("socketutil")
local NetworkMgr = require("ui/network/manager")
local logger = require("logger")
local json = require("json")
local _ = require("gettext")

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
local function todayInfo()
    local t = os.date("!*t", os.time() + UTC_OFFSET)
    local date = string.format("%04d%02d%02d", t.year, t.month, t.day)
    return t, date, SAVE_DIR .. date .. ".png"
end

local function fileExists(path)
    local f = io.open(path, "rb")
    if not f then return false end
    local size = f:seek("end")
    f:close()
    return size and size > 0
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
    self.ui.menu:registerToMainMenu(self)
    self._stopped = false
    self._busy = false
    local configured_dir = G_reader_settings:readSetting(SETTING_SAVE_DIR)
    if type(configured_dir) == "string" and configured_dir:match("^/mnt/us/") then
        SAVE_DIR = configured_dir:gsub("/*$", "") .. "/"
    end
    util.makePath(SAVE_DIR)
    if autoScreensaverEnabled() then configureScreensaver() end
    self:_schedule(STARTUP_DELAY)
end

function Yiyan:_schedule(delay)
    if self._stopped then return end
    UIManager:scheduleIn(delay, function()
        if self._stopped then return end
        self:refreshDaily(false)
        self:_schedule(CHECK_INTERVAL)
    end)
end

function Yiyan:onResume()
    if not self._stopped then self:refreshDaily(false) end
end

function Yiyan:onCloseWidget()
    self._stopped = true
end

function Yiyan:requestSentence(callback)
    if active_worker then
        callback(nil, "busy")
        return
    end
    local pid, read_fd
    local started, start_err = pcall(function()
        pid, read_fd = ffiutil.runInSubProcess(function(_, child_write_fd)
            local result
            local ok, err = pcall(function()
                socketutil:set_timeout(HTTP_TIMEOUT, HTTP_TIMEOUT)
                local chunks = {}
                local request_ok, code = http.request{
                    url = "https://v1.hitokoto.cn/?encode=json",
                    method = "GET",
                    headers = { ["User-Agent"] = "KOReader-yiyan.koplugin/1.0" },
                    redirect = true,
                    sink = function(chunk, sink_err)
                        if sink_err then return nil, sink_err end
                        if chunk then chunks[#chunks + 1] = chunk end
                        return 1
                    end,
                }
                if not request_ok then
                    result = "ERR\nnetwork_error"
                elseif tonumber(code) ~= 200 then
                    result = "ERR\nhttp_" .. tostring(code or "unknown")
                else
                    result = "OK\n" .. table.concat(chunks)
                end
            end)
            if not ok then result = "ERR\n" .. tostring(err) end
            ffiutil.writeToFD(child_write_fd, result or "ERR\nnetwork_error", true)
        end, true)
    end)
    if not started or not pid then
        logger.warn("Yiyan: cannot start request worker", tostring(start_err or read_fd))
        callback(nil, "network_error")
        return
    end

    active_worker = { pid = pid, read_fd = read_fd, callback = callback,
        deadline = os.time() + HTTP_TIMEOUT, timed_out = false }
    UIManager:preventStandby()

    local function poll()
        local worker = active_worker
        if not worker or worker.pid ~= pid then return end
        if not ffiutil.isSubProcessDone(pid) then
            if not worker.timed_out and os.time() >= worker.deadline then
                worker.timed_out = true
                ffiutil.terminateSubProcess(pid)
            end
            UIManager:scheduleIn(POLL_INTERVAL, poll)
            return
        end

        active_worker = nil
        UIManager:allowStandby()
        local read_ok, response = pcall(ffiutil.readAllFromFD, worker.read_fd)
        if worker.timed_out then
            worker.callback(nil, "timeout")
        elseif not read_ok or type(response) ~= "string" then
            worker.callback(nil, "network_error")
        elseif response:sub(1, 3) == "OK\n" then
            local decoded_ok, payload = pcall(json.decode, response:sub(4))
            if decoded_ok and type(payload) == "table" and type(payload.hitokoto) == "string" and payload.hitokoto ~= "" then
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
    local width, height = Device.screen:getWidth(), Device.screen:getHeight()
    local image = Blitbuffer.new(width, height, Blitbuffer.TYPE_BB8)
    image:fill(Blitbuffer.COLOR_WHITE)

    local lines = { payload.hitokoto }
    local source = type(payload.from) == "string" and payload.from or ""
    local author = type(payload.from_who) == "string" and payload.from_who or ""
    if author ~= "" then
        source = author .. (source ~= "" and (" · " .. source) or "")
    end
    if source and source ~= "" then table.insert(lines, "\n\n—— " .. source) end
    table.insert(lines, "\n\n" .. date:sub(1, 4) .. "-" .. date:sub(5, 6) .. "-" .. date:sub(7, 8) .. " · 一言")

    local tmp = target .. ".tmp"
    os.remove(tmp)
    local text
    local rendered, written = pcall(function()
        local font_size = math.max(20, math.floor(width / 18))
        local size
        repeat
            if text then text:free() end
            text = TextBoxWidget:new{
                text = table.concat(lines),
                face = Font:getFace("cfont", font_size),
                width = math.floor(width * 0.82),
                alignment = "center",
                auto_para_direction = true,
            }
            -- KOReader returns a geometry object, not separate width/height.
            -- Leave height unset so all wrapped lines are rendered.
            size = text:getSize()
            if size.h <= height * 0.80 or font_size <= 14 then break end
            font_size = font_size - 2
        until false
        text:paintTo(image, math.floor((width - size.w) / 2), math.floor((height - size.h) / 2))
        return image:writeToFile(tmp, "png", 90, true)
    end)
    if text then text:free() end
    image:free()
    if not rendered or not written then
        logger.warn("Yiyan: image rendering failed", tostring(written))
        os.remove(tmp)
        return false
    end
    local renamed = os.rename(tmp, target)
    if not renamed then os.remove(tmp); return false end
    return true
end

function Yiyan:cleanupOldImages(keep_path)
    local ok, lfs = pcall(require, "libs/libkoreader-lfs")
    if not ok then return end
    for name in lfs.dir(SAVE_DIR) do
        if name:match("^%d%d%d%d%d%d%d%d%.png$") then
            local path = SAVE_DIR .. name
            if path ~= keep_path and lfs.attributes(path, "mode") == "file" then os.remove(path) end
        end
    end
end

function Yiyan:refreshDaily(show_message)
    if self._stopped or self._busy or active_worker then return end
    util.makePath(SAVE_DIR)
    local _, date, image_path = todayInfo()
    if fileExists(image_path) then return end

    -- Do not consume the daily attempt while Wi-Fi is disconnected; the periodic
    -- check can start it after connectivity returns. A network request itself
    -- is still capped to one per Beijing calendar date.
    if not networkIsConnected() then
        if show_message then UIManager:show(InfoMessage:new{ text = _("网络未连接，今日请求尚未发出") }) end
        return
    end
    if G_reader_settings:readSetting(SETTING_LAST_ATTEMPT) == date then
        if show_message then UIManager:show(InfoMessage:new{ text = _("今天已请求过一言，明天再试") }) end
        return
    end

    G_reader_settings:saveSetting(SETTING_LAST_ATTEMPT, date)
    G_reader_settings:flush()
    self._busy = true
    self:requestSentence(function(payload, err)
        self._busy = false
        if not payload then
            logger.warn("Yiyan: daily request failed", tostring(err))
            if show_message and not self._stopped then UIManager:show(InfoMessage:new{ text = _("今日一言请求失败；为避免重复请求，明天再试") }) end
            return
        end
        local _, current_date = todayInfo()
        if current_date ~= date then return end
        if not self:renderImage(payload, date, image_path) then
            if show_message and not self._stopped then UIManager:show(InfoMessage:new{ text = _("无法保存一言壁纸图片") }) end
            return
        end
        self:cleanupOldImages(image_path)
        if show_message and not self._stopped then UIManager:show(InfoMessage:new{ text = _("今日一言壁纸已生成") }) end
    end)
end

function Yiyan:chooseOutputDirectory()
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
                    local _, date, old_image = todayInfo()
                    SAVE_DIR = path:gsub("/*$", "") .. "/"
                    G_reader_settings:saveSetting(SETTING_SAVE_DIR, SAVE_DIR)
                    G_reader_settings:flush()
                    util.makePath(SAVE_DIR)
                    local new_image = SAVE_DIR .. date .. ".png"
                    if old_image ~= new_image and fileExists(old_image) and not fileExists(new_image) then
                        ffiutil.copyFile(old_image, new_image)
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
            { text = _("设置屏保图片目录"), callback = function() self:chooseOutputDirectory() end },
        },
    }
end

return Yiyan
