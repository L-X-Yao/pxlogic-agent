# 0.1.0 发布清单

设备回归通过后执行。当前版本是 `0.1.0rc1`，PyPI 上已占名。

## 发布前

- [ ] `docs/REGRESSION.md` 全部通过，16 通道如果可用也跑过。
- [ ] 工作区干净，main 上 CI 全绿。
- [ ] `pyproject.toml` 版本改成 `0.1.0`。
- [ ] README、`skills/pxlogic/SKILL.md` 的安装命令从 `pip install git+...` 改成 `pip install pxlogic-agent`。
- [ ] README “已验证环境”补上日期和版本。
- [ ] 本地跑 `python3 -m unittest discover -s tests -v` 和 `python3 eval/runner.py`。

## 发布

```bash
git checkout -b release-0.1.0
git add pyproject.toml README.md skills/pxlogic/SKILL.md
git commit -m "Release 0.1.0"
git push -u origin release-0.1.0
# CI 全绿后快进合并 main
git checkout main && git merge --ff-only release-0.1.0 && git push origin main
git tag v0.1.0 && git push origin v0.1.0
```

tag 会触发 `.github/workflows/publish.yml`：校验版本、跑测试、build、`twine check --strict`、trusted publishing 上传 PyPI。

## 发布后

- [ ] 干净 venv 里 `pip install -i https://pypi.org/simple pxlogic-agent==0.1.0`，核对版本和 `px_logic.py`。
- [ ] `gh release create v0.1.0`，附更新说明。
- [ ] 在 `molqzone/pxlobster` 开中文 issue 介绍本项目；发出前先给用户看草稿。
- [ ] 同步五处本机副本，逐个 `cmp`。
- [ ] argus-venv 改成 `pip install --upgrade pxlogic-agent`。
- [ ] 之后每次发布先改 `pyproject.toml` 版本号。

注意：本机 pip 全局配置走阿里云镜像，新版本可能延迟同步；验收用官方索引。
