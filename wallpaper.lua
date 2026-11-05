-- Layout measurements are physical pixels, scaled from each bundled template.
local Blitbuffer = require("ffi/blitbuffer")
local ffi = require("ffi")
local Png = require("ffi/png")
local RenderImage = require("ui/renderimage")
local TextBoxWidget = require("ui/widget/textboxwidget")
local Font = require("ui/font")
local Screen = require("device").screen
local logger = require("logger")

local Wallpaper = { VERSION = 7, DEFAULT_STYLE = "classic" }
local BACKGROUNDS = {
    classic = "lyrics-background.png",
    calendar = "date-card-background.png",
    dark = "dark-card-background.png",
}

function Wallpaper.coverStyle(style)
    return BACKGROUNDS[style] and style or Wallpaper.DEFAULT_STYLE
end

function Wallpaper.hasCurrentLayout(record, style)
    return record.layout_version == Wallpaper.VERSION
        and (record.cover_style or Wallpaper.DEFAULT_STYLE) == style
end

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
    local disc_radius = round(56 * scale)
    local text_inset = round(16 * scale)
    local text_x = margin + 2 * disc_radius + text_inset
    local text_width = width - text_inset - text_x
    return {
        body = { x = margin, y = round(height * 0.155), width = width - 2 * margin,
            height = round(height * 0.45), font_size = math.max(20, round(34 * scale)),
            min_size = math.max(14, round(18 * scale)) },
        footer = { y = footer_y, height = footer_height },
        disc = { x = margin + disc_radius, y = footer_y, radius = disc_radius },
        source = { x = text_x, y = footer_y + round(footer_height * 0.22),
            width = text_width, height = round(footer_height * 0.40),
            font_size = math.max(15, round(20 * scale)), min_size = math.max(12, round(14 * scale)),
            bold = true, alignment = "center" },
        date = { x = text_x, y = footer_y + round(footer_height * 0.65),
            width = text_width, height = round(footer_height * 0.27),
            font_size = math.max(11, round(13 * scale)), min_size = math.max(10, round(11 * scale)),
            alignment = "center" },
    }
end

-- The date and dark card backgrounds were composed at the KPW4 resolution.
local function cardLayout(width, height, dark)
    local sx, sy = width / 1072, height / 1448
    local scale = math.min(sx, sy)
    local function box(x, y, w, h, size, min_size, line_height, bold, fgcolor)
        return {
            x = round(x * sx), y = round(y * sy), width = round(w * sx), height = round(h * sy),
            font_size = math.max(1, round(size * scale)), min_size = math.max(1, round(min_size * scale)),
            line_height = line_height, bold = bold, fgcolor = fgcolor,
        }
    end
    local muted = dark and Blitbuffer.COLOR_GRAY_B or Blitbuffer.COLOR_GRAY_6
    return {
        month = not dark and box(149, 210, 235, 145, 108, 76, 0.04, true) or nil,
        day = not dark and box(391, 221, 70, 62, 47, 30, 0.1, true) or nil,
        weekday = not dark and box(464, 229, 181, 58, 32, 24, 0.15, true) or nil,
        header = dark and box(147, 204, 510, 58, 31, 24, 0.16, true) or nil,
        body = dark and box(149, 631, 773, 407, 51, 29, 0.74)
            or box(149, 465, 773, 277, 48, 28, 0.60),
        attribution = not dark and box(150, 759, 770, 52, 29, 22, 0.2, nil, muted) or nil,
        source = box(dark and 296 or 287, 1120, 555, 56, 39, 24, 0.12, true),
        author = box(dark and 297 or 288, 1181, 554, 49, 28, 21, 0.17, nil, muted),
    }
end

local MONTHS = { "Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec." }
local WEEKDAYS = { "日", "一", "二", "三", "四", "五", "六" }
local WEEKDAYS_EN = { "SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT" }
local WEEKDAY_OFFSETS = { 0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4 }

local function cardDate(date)
    local year, month, day = tonumber(date:sub(1, 4)), tonumber(date:sub(5, 6)), tonumber(date:sub(7, 8))
    -- Gregorian weekday from the archived date, independent of device time zone.
    if month < 3 then year = year - 1 end
    local weekday = (year + math.floor(year / 4) - math.floor(year / 100)
        + math.floor(year / 400) + WEEKDAY_OFFSETS[month] + day) % 7 + 1
    return month, weekday
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

local function paintDisc(image, disc)
    local x, y, radius = disc.x, disc.y, disc.radius
    local white, black = Blitbuffer.COLOR_WHITE, Blitbuffer.COLOR_BLACK
    local rim = math.max(1, round(radius / 28))
    local track = round(radius * 0.72)
    -- The white rim keeps the lower half visible on the black footer.
    image:paintCircle(x, y, radius, white)
    image:paintCircle(x, y, radius - rim, black)
    image:paintCircle(x, y, track, white)
    image:paintCircle(x, y, track - math.max(1, round(radius / 56)), black)
    image:paintCircle(x, y, round(radius * 0.27), white)
    image:paintCircle(x, y, round(radius * 0.10), black)
end

local LINE_BREAK = { ["，"] = true, ["。"] = true, ["；"] = true, ["！"] = true, ["？"] = true }
local LINE_BREAK_SUFFIX = {
    ["”"] = true, ["’"] = true, ["」"] = true, ["』"] = true, ["》"] = true,
    ["〉"] = true, ["）"] = true, ["】"] = true, ["〕"] = true, ["〗"] = true,
    ['"'] = true, ["'"] = true, [")"] = true, ["]"] = true, ["}"] = true,
    ["!"] = true, ["?"] = true, ["…"] = true,
}

local function quoteText(payload)
    return (payload.hitokoto:gsub("\r\n", "\n"):gsub("\r", "\n"):gsub("\n+$", ""))
end

function Wallpaper.body(payload)
    local text = quoteText(payload)
    local parts, break_pending = {}, false
    -- Match one UTF-8 character and keep punctuation clusters together.
    for char in text:gmatch(".[\128-\191]*") do
        if break_pending and not LINE_BREAK[char] and not LINE_BREAK_SUFFIX[char] then
            if char ~= "\n" then parts[#parts + 1] = "\n" end
            break_pending = false
        end
        parts[#parts + 1] = char
        if LINE_BREAK[char] then break_pending = true end
    end
    return table.concat(parts)
end

local function textInBox(text, box, fgcolor, bgcolor, default_line_height)
    local size = box.font_size
    -- Layout sizes are already in pixels. Font:getFace scales its input
    -- again, so convert the intended pixel size back to a UI font size.
    local screen_scale = Screen:scaleBySize(1024) / 1024
    while true do
        local at_minimum = size <= box.min_size
        local widget = TextBoxWidget:new{
            text = text, face = Font:getFace("cfont", math.max(1, round(size / screen_scale))),
            width = box.width, bold = box.bold,
            -- KOReader allocates a pixel buffer during construction. Bound it
            -- on every size trial, even when the quote has many line breaks.
            height = box.height,
            height_adjust = true,
            height_overflow_show_ellipsis = true,
            alignment = box.alignment or "left", alignment_strict = true, auto_para_direction = true,
            line_height = box.line_height or default_line_height,
            fgcolor = box.fgcolor or fgcolor,
            bgcolor = bgcolor,
        }
        if at_minimum or (not widget.line_with_ellipsis and widget:getSize().h <= box.height) then
            return widget
        end
        widget:free()
        size = math.max(box.min_size, size - 2)
    end
end

-- The caller provides a private staging path and publishes the validated PNG.
function Wallpaper.render(plugin_path, payload, date, target, width, height, style)
    local image, background, eink
    local widgets = {}
    style = Wallpaper.coverStyle(style)
    local ok, result = pcall(function()
        local black, white = Blitbuffer.COLOR_BLACK, Blitbuffer.COLOR_WHITE
        background = RenderImage:renderImageFile(plugin_path .. "/assets/" .. BACKGROUNDS[style], false)
        assert(background, "Cannot load the bundled wallpaper background")
        background = RenderImage:scaleBlitBuffer(background, width, height)
        assert(background, "Cannot scale the wallpaper background")
        image = Blitbuffer.new(width, height, Blitbuffer.TYPE_BB8)
        image:blitFrom(background, 0, 0, 0, 0, width, height)
        background:free()
        background = nil
        local function paint(text, box, fgcolor, bgcolor, line_height)
            local widget = textInBox(text, box, fgcolor, bgcolor, line_height)
            widgets[#widgets + 1] = widget
            widget:paintTo(image, box.x, box.y)
        end
        local title, author = Wallpaper.source(payload)
        if style == Wallpaper.DEFAULT_STYLE then
            local layout = Wallpaper.layout(width, height)
            -- Paint the black band after scaling for a uniform text background.
            image:paintRect(0, layout.footer.y, width, layout.footer.height, black)
            paintDisc(image, layout.disc)
            paint(Wallpaper.body(payload), layout.body, black, white, 0.65)
            local formatted_date = date:sub(1, 4) .. "-" .. date:sub(5, 6) .. "-" .. date:sub(7, 8)
            paint(title, layout.source, white, black, 0.25)
            paint((author or "一言") .. "  ·  " .. formatted_date, layout.date, white, black, 0.25)
        else
            local dark = style == "dark"
            local layout = cardLayout(width, height, dark)
            local fgcolor = dark and white or black
            local bgcolor = dark and Blitbuffer.COLOR_GRAY_4 or white
            local function cardText(text, box)
                paint(text, box, fgcolor, bgcolor, 0.65)
            end
            local month, weekday = cardDate(date)
            local day = date:sub(7, 8)
            if dark then
                cardText(date:sub(5, 6) .. "." .. day .. "  /  " .. WEEKDAYS_EN[weekday], layout.header)
                cardText(Wallpaper.body(payload), layout.body)
            else
                cardText(MONTHS[month], layout.month)
                cardText(day, layout.day)
                cardText("星期" .. WEEKDAYS[weekday], layout.weekday)
                cardText(quoteText(payload), layout.body)
                cardText("来自一言", layout.attribution)
            end
            cardText(title, layout.source)
            cardText(author or "一言", layout.author)
        end
        eink = Blitbuffer.new(width, height, Blitbuffer.TYPE_BB8)
        eink:ditherblitFrom(image, 0, 0, 0, 0, width, height)
        image:free()
        image, eink = eink, nil
        local called, written, err = image:writeToFile(target, "png")
        assert(called and written ~= false, err or written or "Cannot write the wallpaper PNG")
        assert(Wallpaper.isValidPNG(target), "Incomplete wallpaper PNG")
        return true
    end)
    for _, widget in ipairs(widgets) do widget:free() end
    if background then background:free() end
    if eink then eink:free() end
    if image then image:free() end
    if not ok then
        logger.warn("Yiyan: wallpaper rendering failed", tostring(result))
        return false, tostring(result)
    end
    return result
end

return Wallpaper
