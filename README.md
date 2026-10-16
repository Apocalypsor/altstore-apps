# AltStore / SideStore Source

自动收录 [TiebaPure](https://github.com/infinityf4p/TiebaPure-iOS/releases) 和 [EhPanda](https://github.com/EhPanda-Team/EhPanda/releases)，**包含预发布版本**。

## 添加到 SideStore

在 SideStore → Sources → `+` 中粘贴：

```text
https://raw.githubusercontent.com/Apocalypsor/altstore-apps/release/source.json
```

`main` 保存配置、生成器和 GitHub Actions；`release` 是独立发布分支，只包含 `source.json`。IPA 直接来自上游公开 Release，不修改、不重新打包、不重复托管。

## 自动更新

- 每 6 小时检查一次，也可在 Actions → **Update SideStore source** → **Run workflow** 手动触发；修改生成器或配置并推送 `main` 时也会触发。
- 首次成功运行自动创建 `release` 分支。后续只有 JSON 内容变化时才提交，无需额外 PAT，使用仓库自带的 `GITHUB_TOKEN`。
- 草稿、尚未上传 IPA 的 Release 不收录；正式版和预发布版均收录，在更新说明中标注渠道。
- 每个应用检查最近发布且附有匹配 IPA 的 10 个 Release，读取真实包信息后排序，并按“版本号 + 构建号”去重。可在 `apps.json` 修改 `history_limit`。
- 下载失败、包大小/摘要不一致、Bundle ID 不匹配或无法可靠解析版本时，工作流失败并保留已经发布的源，不会提交半成品。
- 只缓存已验证的 IPA 元数据。资产 ID、更新时间、大小、摘要或 URL 改变会重新下载；不会永久缓存 IPA，也不会把 token 写进 JSON。

GitHub 定时任务可能延迟，且长期无活动的公开仓库可能被 GitHub 暂停定时任务。可在 Actions 页面重新启用。

## 版本规则

SideStore 安装时会比对源中的版本与 IPA，不能直接使用 GitHub 标签或文件名：

| Source 字段 | 数据来源 |
| --- | --- |
| `version` | 主 App `Info.plist` 的 `CFBundleShortVersionString`，原样保留 |
| `buildVersion` | `CFBundleVersion`，保留字符串 |
| `bundleIdentifier` | 校验 `CFBundleIdentifier` 与配置一致 |
| `minOSVersion` | `MinimumOSVersion` |
| `date` | GitHub Release 的 `published_at`，不使用编辑时间 |
| `size` / `sha256` | 实际下载的 IPA，另与 GitHub 资产元数据校验 |
| `appPermissions` | 主 App 与扩展的隐私说明、签名 entitlement 名称 |

排序先比较应用版本，再比较构建号，最后比较发布时间；都是按数字分段比较，保证 `2.7.10 > 2.7.9`。例如 EhPanda `3.0.0 (158)` 和 `2.8.1 (158)` 共用构建号，仍会把 `3.0.0` 放在前面。同一版本的新 build 也能被识别，不能只去重版本号。

不把 `v`、`beta` 等 Release 标签修饰符拼进实际 `version`。Apple 数字版本与 `d` / `a` / `b` / `fc` 构建后缀可排序；遇到未知格式会报错，避免猜测或修改真实版本。

保留历史版本及其各自的最低系统要求，SideStore 可选择列表中的首个兼容版本。EhPanda `3.0.0 (158)` 预发布和 `2.8.1 (158)` 均要求 iOS 26；旧系统需要使用兼容的历史版。历史窗口不保证永久覆盖所有旧系统。

## 本地验证与生成

Python 3.12+，无第三方 Python 依赖。生成需要 macOS 自带的 `codesign` 来读取签名权限，**不会运行 IPA 中的代码**。Actions 使用 `macos-15`。单元测试可在 Linux / macOS 运行。

```sh
python3 -m unittest discover -s tests -v
python3 scripts/update_source.py
```

输出为 `dist/source.json`。本地可选设置 `GH_TOKEN` / `GITHUB_TOKEN` 提高 GitHub API 限额；不要提交 token。`--repository owner/repo` 可指定发布仓库；Actions 自动使用 `GITHUB_REPOSITORY`。

添加应用时，在 `apps.json` 中配置上游仓库、唯一 IPA 匹配规则、预期 Bundle ID、介绍和图标 URL。若多个 IPA 同时匹配，生成器会要求收窄规则，不随意选择安装包。

格式参考：[AltStore Source 规范](https://faq.altstore.io/developers/make-a-source)。
