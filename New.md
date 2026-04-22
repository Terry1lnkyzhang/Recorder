修改一下生成session_YYYYMMDD_HHMMSS目录的逻辑，现在目录改为用testcase_id创建，
目录的第一层是testcase_id：
第二层有原来的session_YYYYMMDD_HHMMSS改为{testcase_id}_{version_number}_{recorder_person}_YYYYMMDD_HHMMSS
其他逻辑不变


我想增加一个逻辑，在AI Checkpoint功能中，
在当前
截图1
截图2的位置右边新加一个按钮 添加历史截图
点击之后可以让用户在当前的session目录下的截图文件夹中选择自己一张已截图的图片文件加到下方



Record Session Viewer页面优化一些逻辑

2.事件列表现在类型、动作两个列要支持筛选，类似与excel表的筛选功能



AI Checkpoint页面布局上做一些更改：
把现在query模块和查询结果相关模块放在一行都要缩小width放在一行左右分布
新加模块
模块名Design Steps外加一个内容框
模块名Step comment外加一个内容框





现在事件列表新加一个功能，所有行都支持删除操作点击右键可以删除，如果这个行的类型为checkpoint那么他还要支持修改操作，右键后多一个修改按钮，点击修改弹出AI CheckPoint窗口，在窗口上改完点击保存后 要把保存的信息替换掉刚才点击的那一行的数据。


Record Session Viewer页面
事件列表新增添加check点功能
所有行都支持右键 插入CheckPoint功能 现在右键之后加一个CheckPoint的按钮，点击之后和修改一样弹出AI CheckPoint窗口，点击保存后，在当前行后面插入这新增的一行数据

是否可以支持插入屏幕录制就是我们开始录制的功能，然后转换成步骤直接加到事件列表里，判断下是否可行

Record Session Viewer页面
事件列表新增check点功能优化
现在的插入录制步骤改为可以延伸出一个二级菜单的按钮，悬浮后延伸出二级按钮，
原有的插入录制步骤名字改为插入步骤，原有的逻辑取消，延伸出二级按钮菜单：录制；导入
点击录制延续现在的逻辑就是把原有的逻辑从插入录制步骤移到 插入步骤-录制
导入暂时先不做

导入逻辑 我想做成点击导入后弹出选择session 和第一个界面的导入并续录的页面类似，点击后可以把这个session文件的步骤全部导入进来到这个插入位置




1当前AI checkpoint中如果有step description和design steps的话，就都一起给到ai了，不应该这样   pass
2.应该增加一个clear或者删除的图标，情况一下跟ai的历史对话记录，当前所有的都删除之后，在问AI，   pass
3.当前给AI选空提示词的时候  ai 返回的内容为啥还是有作为自动化分析助手   
4.当前跟AI对话的时候是否会把之前多轮的对话都发给AI，如果是多轮发送，应该改成单轮，只发送当前     pass
5.Setting当前没有默认的连接到mysql的  获取不到提示词  而且 setting页面太长了  看不到保存按钮，应该增加scroll
  
2.AI checkpoint 添加快捷键，保存之后自动最小化recorder    低    pass
3.recorder加一个等待xxx出现的功能  高         
6.AI 检查点比如要用两个图的时候，应该先有一个步骤笔记截图1，然后再有个步骤截图2，这两个截图就是后续要用的   中  pass
8.当前Session都存哪里了，加一个网络路径   高    pass
9.手动导入plan 就是design step  低              
 
testcaseid输入之后  连接到数据库中看看是不是有自动化脚本  有的话就提示   pass
 
AI checkpoint 的query框是不是能下拉一些，小面好像有一些控件是浪费掉了   pass
 
AI checkpoint 增加一个停止功能，当query 有时候会卡主，当前没有停止



我想在开始录制的时候 在我的桌面上生成一个悬浮窗用于展示Design steps


悬浮窗更改：
1.颜色再透明一点。
2.将Design Steps拆分一下，输入的文本按照 每句文本前的 序号如 2. 3.这样做一下拆分，（但是要注意序号可能重复，也可能没按照正常顺序来排序，不要管，仅用作拆分使）
3.拆分之后悬浮窗内仅展示一句拆分后的Design Steps
4.悬浮穿左右两侧分别加一个 向左 向右的符号，点击后可以切换拆分后的DesignSteps
 
 当前主页面上的settings功能按钮 要换成一个设置图片放在右上角，而且 setting页面太长了  看不到保存按钮，应该增加scroll

 {
  "event_type": "wait",
  "action": "wait_for_image",
  "screenshot": "screenshots/wait_0001.png",
  "mouse": {},
  "keyboard": {},
  "media": [
    {
      "type": "image",
      "path": "screenshots/wait_0001.png",
      "region": {
        "left": 100,
        "top": 200,
        "width": 320,
        "height": 80
      }
    }
  ],
  "additional_details": {
      "wait_timeout_seconds": 30
    }
  }
}

{
  "event_id": "wait_0371",
  "timestamp": "2026-04-17T10:22:22.748781+08:00",
  "event_type": "wait",
  "action": "wait_for_image",
  "screenshot": "screenshots/wait_0056.png",
  "mouse": {},
  "keyboard": {},
  "scroll": {},
  "window": {
    "title": "添加等待图片",
    "class_name": "TkTopLevel",
    "handle": "0x500a40",
    "process_id": 38448,
    "process_name": "python.exe"
  },
  "ui_element": {
    "name": "",
    "control_type": "",
    "automation_id": "",
    "class_name": "",
    "help_text": "",
    "rectangle": {}
  },
  "note": "等待此区域中的目标图片出现",
  "checkpoint": {},
  "media": [
    {
      "type": "image",
      "path": "screenshots/wait_0056.png",
      "region": {
        "left": 219,
        "top": 188,
        "right": 577,
        "bottom": 551,
        "width": 358,
        "height": 363
      }
    }
  ],
  "ai_result": {},
  "additional_details": {
    "source": "user",
    "wait_timeout_seconds": 120
  }
}


1. 添加等待图片的按钮 文字改为  添加等待事件
2. 添加等待图片的窗口上方的文字改为  添加等待事件
3. 现在添加等待图片的窗口整体的布局再优化一下，现在保存与取消的上方有一条断断续续的黑线不知道是什么

开始录制的弹出 录制元数据的窗口增加新逻辑
1. Project 下拉框
下拉框内容 Taichi、Kylin、Earth_Kylin、 Earth_Taichi、 Earth
2.BaselineName 下拉框
下拉框的数据来源：
读取数据库baselinetable表
获取BaseLineName列并去重得到最后的数据


现在最后生成的数据

Project
BaselineName 这两个元素要放在testcaseid上面
project默认为Taichi
BaselineName默认为空，并允许为空



102 x 9
现在生成路径的规则是：

根目录固定在网络共享目录
见 runtime_paths.py:8-26

当前根目录是：

Recordings
第一层目录用 testcase_id
见 session.py:76-85

第二层目录名规则是：
t
e
s
t
c
a
s
e
_
i
d
_
v
e
r
s
i
o
n
_
n
u
m
b
e
r
_
r
e
c
o
r
d
e
r
_
p
e
r
s
o
n
_
Y
Y
Y
Y
M
M
D
D
_
H
H
M
M
S
S
testcase_id_version_number_recorder_person_YYYYMMDD_HHMMSS

代码在这里： session.py:80-85

也就是最终完整路径规则为：

Recordings根目录
 
/
 
testcase_id
 
/
 现在改成
testcase_id_version_number_recorder_person_时间戳
Recordings根目录/testcase_id/project/testcase_id_version_number_recorder_person_时间戳
test