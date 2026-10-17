<h1>预告：手机端Agent即将上线，请尽请期待！</h1>
<h1>捐赠赞助，支持一下吧！</h1>
<image src="https://github.com/anonymous123456988/NeuraAgent-v3/blob/main/%E6%8D%90%E8%B5%A0.jpg">
该版本增加了扩展功能，使AI可以直接调用扩展来完成指令，一下使扩展开发方法：
# NeuraAgent PC 端扩展包开发 README

> 
> 适用：NeuraAgent 电脑桌面版
> 扩展载体：`.zip` 压缩包；Agent 运行时临时解压读取
> 核心机制：AI 检索扩展`manifest.json`中的`title`（用途标题）匹配用户指令；**只有标记为【启用】的扩展才会被检索与调用**；匹配成功后 AI 读取本`readme.md`学习调用规则，执行`main.py`入口函数。

## 目录结构规范

> 
> src 文件夹为**可选**，小型扩展不需要创建 src，不要多余文件夹，避免路径读取异常

### 小型扩展（推荐，邮件、简易工具类，无 src）

```
your_extension.zip
├ manifest.json
├ readme.md
└ main.py
```

### 大型扩展（代码拆分多文件，可选增加 src 目录）

```
your_extension.zip
├ manifest.json
├ readme.md
├ main.py
└ src/
    ├ helper.py
    └ utils.py
```

⚠️ **打包强制规则**

1. 压缩包根目录直接放置文件，**不要将所有文件嵌套在一层同名文件夹内**（嵌套文件夹是扩展加载失败最常见原因）
2. 简单扩展不要额外创建 src 文件夹，多余目录会造成 AI 读取路径错误

---

## manifest.json（必填，扩展元信息）

`title`是**用途标题**，AI 依靠此字段检索匹配用户指令，关键词尽量简洁明确

```
{
    "title": "发送邮件",
    "description": "使用预设授权码发送邮件，支持填写收件地址、邮件标题与正文内容",
    "version": "1.0.0",
    "author": "开发者",
    "enable_auto_load": true
}
```

字段说明

- `title`：核心检索字段，AI 检索启用扩展时，比对该文本和用户指令
- `description`：扩展描述，用于扩展管理面板展示
- `version`：扩展版本号
- `author`：作者名称
- `enable_auto_load`：布尔值，true 代表导入后默认启用

---

## readme.md（必填，供 AI 阅读学习调用规则）

> 
> 这份文档面向 AI，AI 匹配到扩展标题后会完整读取本文档，理解功能、入参、返回格式与调用示例

```
# Neura扩展调用说明（AI读取）
## 功能
发送邮件，填写收件邮箱、邮件标题、邮件正文，使用预设授权码完成发送。

## 触发规则
当用户指令与本扩展manifest内title【发送邮件】匹配时，调用本扩展。
统一入口函数：run_task(receiver, subject, content)

### 参数定义
receiver: string，收件人的邮箱地址
subject: string，邮件的标题
content: string，邮件正文内容

### 返回数据格式
执行成功：{"status":"ok","msg":"邮件发送完成"}
执行失败：{"status":"error","msg":"错误详细文本"}

### 使用示例
用户指令：给test@example.com发送邮件，标题为项目汇报，正文写项目已经开发完成
AI调用：run_task(receiver="test@example.com", subject="项目汇报", content="项目已经开发完成")
```

---

## main.py（必填，扩展执行入口代码）

> 
> 固定入口函数名称：`run_task()`，**不可修改函数名称**，Agent 会调用该函数执行扩展

```
def run_task(receiver: str, subject: str, content: str):
    """
    NeuraAgent扩展统一入口
    """
    try:
        # 此处写入你的业务代码
        return {"status":"ok","msg":"邮件发送完成"}
    except Exception as e:
        return {"status":"error","msg":str(e)}
```

---

## 扩展完整加载流程

1. 用户向 Agent 下达指令
2. Agent 遍历所有**已启用**的扩展，读取每个扩展`manifest.json`的 title，检索匹配用户指令
3. 匹配成功，自动读取该扩展内`readme.md`，学习调用参数、格式、使用限制
4. AI 按照 readme 描述，生成参数调用`main.py`的`run_task()`函数
5. 获取扩展返回的 JSON 结果，交给 AI 继续处理对话任务

## 打包与导入步骤

1. 选中`manifest.json`、`readme.md`、`main.py`三个文件，右键直接压缩成 zip 包
2. 打开 NeuraAgent 电脑版，进入【扩展管理】页面，点击添加扩展，上传 zip 压缩包
3. 在扩展列表，打开该扩展的启用开关，扩展即可被 AI 检索调用

## 重要注意事项

1. src 目录仅大型多文件扩展可选，简单工具类扩展不要添加 src 目录
2. 入口函数名固定为`run_task()`，改名会导致 Agent 无法调用扩展
3. 禁用状态下的扩展，AI 直接跳过，不会读取任何文件
4. 扩展拥有本地代码执行权限，代码内建议增加安全校验，避免误操作系统关键文件
5. 调试排查：在扩展管理面板查看扩展日志，可以查看 AI 是否成功读取 readme、识别扩展标题、捕获执行报错

## 故障排查清单

1. AI 完全不调用扩展
   - 确认扩展已经开启【启用】开关
   - 检查 zip 包是否嵌套外层文件夹
   - 核对 manifest 的 title 关键词，尽量和用户指令用词匹配
2. 调用扩展时报错
   - 确认 main.py 入口函数名称是`run_task()`
   - 检查 readme 文档里的参数定义，AI 会严格按照文档传递参数，文档与代码参数不一致会报错
3. 扩展导入失败
   - 确认压缩包根目录存在 manifest.json，文件名大小写不要写错
