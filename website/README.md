# 实验网站

网站使用 Material for MkDocs，内容位于 `docs/`。在 `website/` 中独立安装网站依赖、预览和构建。

## 安装与预览

从实验仓库根目录进入网站：

```bash
cd website
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m mkdocs serve --dev-addr 127.0.0.1:8000
```

在浏览器打开终端显示的地址。修改 Markdown 后，预览自动更新；按 Ctrl+C 停止服务。

再次预览时，只需进入 `website/`、激活已有环境并运行 `mkdocs serve`。

## 内容组织

- `docs/index.md`：实验主页。
- `docs/preparation/`：环境、终端和测量说明。
- `docs/lab1/`～`docs/lab5/`：实验介绍、教程、操作页与核心代码讲解。
- `docs/faq.md`、`docs/glossary.md`：常见问题与术语。
- `mkdocs.yml`：导航、主题和网站地址。
- `overrides/main.html`、`docs/stylesheets/extra.css`：页顶提示与样式。
- `hooks.py`：在构建时引入各实验 README 和代码片段，并生成独立源码压缩包。
- `search_dictionary.txt`：中文搜索使用的课程术语词典。

操作页中的 `{{LAB1_README}}` 等标记会在构建时替换为对应 README 的正文。更新构建命令或参数时，修改 Lab 内的 README 即可。

核心代码讲解位于 `docs/lab1/code.md` 等页面，使用下面的标记引用代码：

```text
{{SOURCE {"path": "Lab1/src/partition.cpp"}}}
{{SOURCE {"path": "Lab1/src/benchmark.cpp", "start": "    omp_set_dynamic(0);", "end": "    std::vector<double> times, checksums;"}}}
```

`path` 相对于仓库根目录。省略 start/end 时展示完整文件；指定时包含起止行，start 必须在文件中唯一，end 取其后首次匹配。构建会生成原文件行号与 GitHub 源码链接，缺失路径或定位文本会使构建失败。修改实现后应同时复核相邻讲解是否仍然准确。

源码下载来自当前 Lab 文件，包含源码、构建配置、Python 脚本及其依赖和第三方文件；排除 `build/`、`results/`、`.venv/`、波形与 Python 缓存。下载文件在构建时生成，无需额外维护压缩包。

## 构建静态页面

```bash
python3 -m mkdocs build --strict
```

输出位于 `website/site/`。本命令只构建文档，不编译或运行 Lab。

## GitHub Pages 部署

使用现有的公开仓库：

```text
https://github.com/MAdrid1011/parallel-computing-labs
```

对应的项目主页地址为：

```text
https://madrid1011.github.io/parallel-computing-labs/
```

`mkdocs.yml` 已按这个项目路径配置 `site_url`。

### 1. 准备源码仓库

先将审核完成的实验与网站源码提交到仓库的源码分支，保留 `Lab1/`～`Lab5/` 与 `website/` 的同级关系。`origin` 应指向上面的仓库。

网站的 `site/` 和各实验的构建、测量数据不需要放进源码分支。执行下一步前，确认用于发布的本地源码已经是希望学生获得的版本。

### 2. 发布静态页面分支

当前仓库已经有远程 `gh-pages`。每次发布前先同步这个远程分支的引用，让生成的页面提交沿用最新发布历史；这对只克隆了 `main` 的副本尤其重要。

在 `website/` 中，激活环境后执行：

```bash
git fetch origin gh-pages:refs/remotes/origin/gh-pages
python3 -m mkdocs gh-deploy --strict --remote-name origin --remote-branch gh-pages
```

这条命令会构建网站，并将生成页面推送到远程 `gh-pages` 分支。它是实际发布操作；仅查看本地效果时，使用前面的 `serve` 或 `build`。

### 3. 设置 Pages 来源

打开 GitHub 仓库的 **Settings → Pages**：

1. 在 **Build and deployment → Source** 选择 **Deploy from a branch**。
2. Branch 选择 **gh-pages**。
3. Folder 选择 **/(root)**。
4. 点击 **Save**，等待 Pages 发布，并访问上面的项目主页地址。

### 4. 更新网站

修改并保存源码，在本地预览确认后，提交并推送源码，再从 `website/` 重新执行上面的远程分支同步与 `gh-deploy` 两条命令。

此方式不添加自定义 GitHub Actions 构建或实验检查文件。GitHub Pages 自身仍使用平台的部署流程发布静态页面。

部署方法参见 [Material 的手动发布文档](https://squidfunk.github.io/mkdocs-material/publishing-your-site/#with-mkdocs)和 [GitHub Pages 来源配置文档](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)。
