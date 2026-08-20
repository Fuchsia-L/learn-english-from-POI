"""extension/manifest.json 的形状（工单 17-6）。

不需要浏览器、不需要 node —— 纯读文件，所以**任何机器上都真跑**。
存在的理由：manifest 坏掉曾经会被 E2E 夹具当成"环境问题"跳过去，7 个用例
既没红也没跑，还被计成通过。现在 E2E 那边改判失败了，这里再加一层不依赖
任何外部组件的守门：双 background 键、权限最小、内容脚本只在 http(s) 上跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

MANIFEST = Path(__file__).resolve().parents[1] / "extension" / "manifest.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_is_valid_json_mv3(manifest: dict):
    assert manifest["manifest_version"] == 3
    assert manifest["name"] and manifest["version"]


def test_background_keeps_both_browsers(manifest: dict):
    """Chrome 认 service_worker，Firefox 121+ 认 scripts —— 两个键都得在。

    少哪个都是"在那个浏览器上装得上、但一个网络请求也发不出去"。
    """
    bg = manifest["background"]
    assert bg["service_worker"] == "bg.js"          # Chrome / Edge
    assert bg["scripts"] == ["bg.js"]               # Firefox 121+
    gecko = manifest["browser_specific_settings"]["gecko"]
    assert gecko["id"] and gecko["strict_min_version"] == "121.0"


def test_permissions_stay_minimal(manifest: dict):
    """权限最小：一个 permissions 都不要，host 只到本机两个名字。"""
    assert manifest.get("permissions", []) == []
    assert sorted(manifest["host_permissions"]) == [
        "http://127.0.0.1/*",
        "http://localhost/*",
    ]


def test_content_script_only_on_http_pages(manifest: dict):
    cs = manifest["content_scripts"]
    assert len(cs) == 1
    assert sorted(cs[0]["matches"]) == ["http://*/*", "https://*/*"]
    assert cs[0]["js"] == ["content.js"]
    assert cs[0]["all_frames"] is False


def test_declared_files_exist(manifest: dict):
    """manifest 里点名的文件必须真的在（改名/漏推立刻响）。"""
    root = MANIFEST.parent
    named = {
        manifest["background"]["service_worker"],
        *manifest["background"]["scripts"],
        *manifest["content_scripts"][0]["js"],
        *manifest["web_accessible_resources"][0]["resources"],
    }
    missing = sorted(n for n in named if not (root / n).is_file())
    assert missing == [], f"manifest 点名了但文件不在: {missing}"
