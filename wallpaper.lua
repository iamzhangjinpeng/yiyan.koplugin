-- White lyric-poster layout. All measurements are physical pixels, scaled from
-- the bundled 758x1024 template; text never overlaps the corner illustration.
local Blitbuffer = require("ffi/blitbuffer")
local ffi = require("ffi")
local Png = require("ffi/png")
local RenderImage = require("ui/renderimage")
local TextBoxWidget = require("ui/widget/textboxwidget")
local Font = require("ui/font")
local Screen = require("device").screen
local logger = require("logger")

local Wallpaper = { VERSION = 4 }

function Wallpaper.isValidPNG(path)
    local file = io.open(path, "rb")
    if not file then return false end
    local head = file:read(8)
    local size = file:seek("end")
    local tail
    if size and size >= 32 and file:seek("end", -12) then
        tail = file:read(12)
    end
    file:close()
    if head ~= "\137PNG\r\n\26\n" or tail ~= "\0\0\0\0IEND\174\66\96\130" then
        return false
    end
    local called, decoded, image = pcall(Png.decodeFromFile, path, 1)
    if not called or not decoded then return false end
    ffi.C.free(image.data)
    return true
end

local function round(value) return math.floor(value + 0.5) end

function Wallpaper.layout(width, height)
    local scale = math.min(width / 758, height / 1024)
    local margin = round(width * 0.112)
    local footer_y = round(height * 0.90)
    local footer_height = height - footer_y
    local icon_width = round(48 * scale)
    local icon_height = round(32 * scale)
    local text_x = margin + icon_width + round(16 * scale)
    return {
        body = { x = margin, y = round(height * 0.155), width = width - 2 * margin,
            height = round(height * 0.45), font_size = math.max(20, round(34 * scale)),
            min_size = math.max(14, round(18 * scale)) },
        footer = { y = footer_y, height = footer_height },
        cassette = { x = margin, y = footer_y + round((footer_height - icon_height) / 2),
            width = icon_width, height = icon_height },
        source = { x = text_x, y = footer_y + round(footer_height * 0.22),
            width = width - margin - text_x, height = round(footer_height * 0.40),
            font_size = math.max(15, round(20 * scale)), min_size = math.max(12, round(14 * scale)),
            bold = true },
        date = { x = text_x, y = footer_y + round(footer_height * 0.65),
            width = width - margin - text_x, height = round(footer_height * 0.27),
            font_size = math.max(11, round(13 * scale)), min_size = math.max(10, round(11 * scale)) },
    }
end

local function field(value)
    if type(value) ~= "string" then return "" end
    return value:gsub("[%z\1-\31\127]", " "):match("^%s*(.-)%s*$")
end

function Wallpaper.source(payload)
    local author, source = field(payload.from_who), field(payload.from)
    if source ~= "" then return source, author ~= "" and author or nil end
    if author ~= "" then return author end
    return "每日一句"
end

local function paintCassette(image, box)
    local x, y, width, height = box.x, box.y, box.width, box.height
    local white, black = Blitbuffer.COLOR_WHITE, Blitbuffer.COLOR_BLACK
    local stroke = math.max(1, round(width / 24))
    local radius = math.max(1, round(height * 0.15))
    local reel_y = y + round(height * 0.41)
    local left_reel = x + round(width * 0.30)
    local right_reel = x + round(width * 0.70)

    image:paintRect(x, y, width, height, white)
    image:paintRect(x + stroke, y + stroke, width - 2 * stroke, height - 2 * stroke, black)
    image:paintRect(left_reel, reel_y, right_reel - left_reel, stroke, white)
    for _, reel_x in ipairs({ left_reel, right_reel }) do
        image:paintCircle(reel_x, reel_y, radius, white)
        image:paintCircle(reel_x, reel_y, math.max(1, radius - stroke), black)
    end
    local slot_width = round(width * 0.46)
    image:paintRect(x + round((width - slot_width) / 2), y + round(height * 0.77), slot_width, stroke, white)
end

function Wallpaper.body(payload)
    local text = payload.hitokoto:gsub("\r\n", "\n"):gsub("\r", "\n")
    -- Keep the original punctuation while making Chinese phrases read like
    -- lyric lines. The widget still wraps phrases that exceed the safe width.
    for _, mark in ipairs({ "，", "。", "；", "！", "？" }) do
        text = text:gsub(mark .. "\n*", mark .. "\n")
    end
    text = text:gsub("\n+$", "")
    return text
end

local function textInBox(text, box, is_footer)
    local size = box.font_size
    -- Layout sizes are already in pixels. Font:getFace scales its input
    -- again, so convert the intended pixel size back to a UI font size.
    local screen_scale = Screen:scaleBySize(1024) / 1024
    while true do
        local at_minimum = size <= box.min_size
        local widget = TextBoxWidget:new{
            text = text, face = Font:getFace("cfont", math.max(1, round(size / screen_scale))),
            width = box.width, bold = box.bold,
            height = at_minimum and box.height or nil,
            height_overflow_show_ellipsis = at_minimum,
            alignment = "left", alignment_strict = true, auto_para_direction = true,
            line_height = is_footer and 0.25 or 0.65,
            fgcolor = is_footer and Blitbuffer.COLOR_WHITE or Blitbuffer.COLOR_BLACK,
            bgcolor = is_footer and Blitbuffer.COLOR_BLACK or Blitbuffer.COLOR_WHITE,
        }
        if at_minimum or widget:getSize().h <= box.height then return widget end
        widget:free()
        size = math.max(box.min_size, size - 2)
    end
end

function Wallpaper.render(plugin_path, payload, date, target, width, height)
    local image, background, eink
    local widgets = {}
    local tmp = target .. ".tmp"
    os.remove(tmp)
    local ok, result = pcall(function()
        local layout = Wallpaper.layout(width, height)
        background = RenderImage:renderImageFile(plugin_path .. "/assets/lyrics-background.png", false)
        assert(background, "Cannot load the bundled wallpaper background")
        background = RenderImage:scaleBlitBuffer(background, width, height)
        assert(background, "Cannot scale the wallpaper background")
        image = Blitbuffer.new(width, height, Blitbuffer.TYPE_BB8)
        image:blitFrom(background, 0, 0, 0, 0, width, height)
        background:free()
        background = nil
        -- Paint a precise black band after scaling so footer glyphs have a
        -- uniform background at every Kindle resolution.
        image:paintRect(0, layout.footer.y, width, layout.footer.height, Blitbuffer.COLOR_BLACK)
        paintCassette(image, layout.cassette)
        local function paint(text, box, footer)
            local widget = textInBox(text, box, footer)
            widgets[#widgets + 1] = widget
            widget:paintTo(image, box.x, box.y)
        end
        paint(Wallpaper.body(payload), layout.body, false)
        local title, author = Wallpaper.source(payload)
        local formatted_date = date:sub(1, 4) .. "-" .. date:sub(5, 6) .. "-" .. date:sub(7, 8)
        paint(title, layout.source, true)
        paint((author or "一言") .. "  ·  " .. formatted_date, layout.date, true)
        eink = Blitbuffer.new(width, height, Blitbuffer.TYPE_BB8)
        eink:ditherblitFrom(image, 0, 0, 0, 0, width, height)
        image:free()
        image, eink = eink, nil
        local called, written, err = image:writeToFile(tmp, "png")
        assert(called and written ~= false, err or written or "Cannot write the wallpaper PNG")
        assert(Wallpaper.isValidPNG(tmp), "Incomplete wallpaper PNG")
        assert(os.rename(tmp, target), "Cannot replace the wallpaper PNG")
        return true
    end)
    for _, widget in ipairs(widgets) do widget:free() end
    if background then background:free() end
    if eink then eink:free() end
    if image then image:free() end
    if not ok then
        os.remove(tmp)
        logger.warn("Yiyan: wallpaper rendering failed", tostring(result))
        return false, tostring(result)
    end
    return result
end

return Wallpaper
