# yiyan.koplugin · 一言壁纸

为 Kindle 上的 KOReader 每天获取一句[一言](https://developer.hitokoto.cn/sentence/)，生成设备分辨率的灰度 PNG，并可自动把 KOReader 睡眠屏保切换为这张壁纸。

## 安装

把整个 `yiyan.koplugin` 目录复制到：

```text
/mnt/us/koreader/plugins/yiyan.koplugin/
```

完全退出并重启 KOReader。插件启动后会延迟几秒检查当天壁纸；联网请求在后台执行，不会卡住阅读界面。

## 每日请求限制

- 按北京时间归档，每个日期最多向一言 API 发起一次请求。
- 发起请求前会立即保存日期记录，避免重启后重复请求。请求失败后当天不重试。
- 如果当天图片已存在，直接使用图片，不请求 API。
- 未连接网络时不消耗当天请求额度；插件会定时检查，网络恢复后再尝试。

## 自动屏保

插件首次启动默认启用自动屏保配置：屏保模式设为随机图片、屏保目录指向插件目录、图片拉伸铺满屏幕，并隐藏睡眠文字。修改前的屏保设置会备份；关闭菜单里的「自动配置 KOReader 屏保」会恢复原设置。

图片默认保存到：

```text
/mnt/us/koreader/screenshots/yiyan/YYYYMMDD.png
```

屏保目录只有当前日期的 PNG。插件生成新壁纸后会清理该专用目录中的旧日期图片，不会触碰其他目录。可在「工具 → 一言壁纸 → 设置屏保图片目录」更改目录。

## 手动操作

在「工具 → 一言壁纸」菜单可以关闭或开启自动屏保配置，也可以手动触发今日壁纸生成。手动操作遵守相同的每日一次限制。

接口请求为 `https://v1.hitokoto.cn/?encode=json`。图片包含句子、出处和日期。插件负责配置 KOReader 睡眠屏保，不会修改 Kindle 原生锁屏文件。

## 效果预览

下图使用示例数据模拟当前排版；Kindle 上的字体可能略有不同。

![一言锁屏壁纸示例](docs/demo.png)

## 参考

KOReader 接入、后台请求和屏保设置的实现参考 [danxiangli.koplugin](https://github.com/iamzhangjinpeng/danxiangli.koplugin)。句子数据来自[一言](https://hitokoto.cn/)。
