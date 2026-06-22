#!/usr/bin/env python3
"""接入分析（Onboarding Analysis）· 独立测试脚本。

用途：命令行输入一个 git repo URL，完整执行分析链路，
输出结构化结果到本地文件。无需启动 FastAPI 服务、无需写 DB。

用法:
    # 标准模式
    python scripts/test_onboarding_analysis.py \
      --repo-url https://gitlab.example.com/group/project.git \
      --branch master \
      --level standard \
      --output-dir ./analysis_output/

    # 快速模式（少读文件，适合验证连通性）
    python scripts/test_onboarding_analysis.py \
      --repo-url https://gitlab.example.com/group/project.git \
      --level quick

    # 深度模式（多读文件，耗时更长）
    python scripts/test_onboarding_analysis.py \
      --repo-url https://gitlab.example.com/group/project.git \
      --level deep

    # 附带项目描述先验（注入到 Prompt 中）
    python scripts/test_onboarding_analysis.py \
      --repo-url https://gitlab.example.com/group/project.git \
      --description "Java Spring Boot 微服务，核心业务是视频版权检测"

输出:
    <output-dir>/
    ├── file_tree.txt           完整文件树
    ├── selected_files.json     被选中的文件及优先级
    ├── docs_summary.md         文档分析结果
    ├── directory_summaries/    各目录分析 Markdown
    │   ├── __root__.md
    │   ├── src.md
    │   └── ...
    ├── directory_summary_map.md  供审核/综合分析使用的短目录地图
    ├── project_analysis.md     综合分析
    ├── api_contracts.md        API 契约
    ├── evidence.json           读了哪些文件、失败了哪些
    └── stats.json              文件覆盖率、各步骤耗时
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from gitlab.service import (
    CodeAnalyzer,
    GitLabClient,
    _build_file_tree_text,
    _truncate_for_llm,
    filter_relevant_files,
    _file_priority,
)
from core.onboarding_service import (
    ARTIFACT_CONTENT_MAX_CHARS,
    DIRECTORY_ARTIFACT_MAX_CHARS,
    DIRECTORY_ARTIFACT_SNIPPET_CHARS,
    DIRECTORY_SYNTHESIS_INPUT_MAX_CHARS,
    DIRECTORY_SYNTHESIS_SNIPPET_CHARS,
    _analysis_limits,
    _build_directory_digest,
    _collect_code_content_with_evidence,
    _group_files_by_directory,
    _select_document_files,
    _truncate_text,
    ROOT_DIR,
)
from settings import gitlab_config, llm_config


GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _step(msg: str) -> float:
    print(f"\n{GREEN}▶{RESET} {msg}")
    return time.time()


def _done(t0: float) -> float:
    elapsed = time.time() - t0
    print(f"  {DIM}({elapsed:.1f}s){RESET}")
    return elapsed


def run_analysis(
    *,
    repo_url: str,
    branch: str,
    level: str,
    output_dir: Path,
    token: str,
    description: str = "",
) -> dict:
    """执行完整分析链路，结果写入 output_dir，返回统计摘要。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    evidence: dict = {}

    base_url = gitlab_config.resolve_base_url(repo_url)
    client = GitLabClient(base_url=base_url, private_token=token)
    project_path = GitLabClient.extract_project_path(repo_url)
    limits = _analysis_limits(level)

    print(f"\n{'='*60}")
    print(f"  Repo:    {repo_url}")
    print(f"  Branch:  {branch}")
    print(f"  Level:   {level}  {DIM}(docs={limits['docs']}, dirs={limits['dirs']}, "
          f"files_per_dir={limits['files_per_dir']}, max_chars={limits['max_chars']}){RESET}")
    print(f"  Output:  {output_dir}")
    print(f"  LLM:     {llm_config.model} @ {llm_config.base_url}")
    if description:
        print(f"  Desc:    {description[:80]}...")
    print(f"{'='*60}")

    # 1. 获取文件树
    t0 = _step("获取文件树...")
    tree = client.get_file_tree(project_path, ref=branch)
    file_tree_text = _build_file_tree_text(tree)
    tree_files = sorted([item["path"] for item in tree if item.get("type") == "blob"])
    timings["fetch_tree"] = _done(t0)

    (output_dir / "file_tree.txt").write_text(file_tree_text, encoding="utf-8")
    print(f"  文件树共 {len(tree_files)} 个文件")

    # 2. 筛选文档和代码文件
    t0 = _step("筛选文件...")
    doc_files = _select_document_files(tree_files, limits["docs"])
    relevant_files = filter_relevant_files(
        tree,
        extensions=gitlab_config.file_extensions,
        exclude_dirs=gitlab_config.exclude_dirs,
        max_files=gitlab_config.max_total_files,
    )
    timings["select_files"] = _done(t0)

    selected_files_detail = [
        {"path": f, "priority": _file_priority(f)}
        for f in relevant_files
    ]
    (output_dir / "selected_files.json").write_text(
        json.dumps({
            "doc_files": doc_files,
            "relevant_files": selected_files_detail,
            "total_tree_files": len(tree_files),
            "selected_count": len(relevant_files),
            "doc_count": len(doc_files),
            "coverage_pct": round(len(relevant_files) / max(len(tree_files), 1) * 100, 1),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  文档: {len(doc_files)} 个, 代码: {len(relevant_files)} 个 "
          f"({DIM}覆盖率 {len(relevant_files)/max(len(tree_files),1)*100:.1f}%{RESET})")

    if not relevant_files:
        print(f"\n{RED}✗ 未找到可分析的代码文件，中止。{RESET}")
        return {"error": "no relevant files"}

    # 3. 读取文档内容
    t0 = _step(f"读取 {len(doc_files)} 个文档...")
    docs_content, read_doc_files, failed_doc_files = _collect_code_content_with_evidence(
        client, project_path, doc_files,
        ref=branch, max_file_size_kb=gitlab_config.max_file_size_kb,
    )
    timings["read_docs"] = _done(t0)
    print(f"  成功: {len(read_doc_files)}, 失败: {len(failed_doc_files)}")

    # 4. LLM 分析文档
    analyzer = CodeAnalyzer()

    t0 = _step("LLM 分析文档 → 语义基线...")
    docs_input = docs_content or "未发现可用文档。"
    if description:
        docs_input = f"## 项目描述（用户提供的先验信息）\n{description}\n\n{docs_input}"
    docs_summary = analyzer.analyze_documentation(
        _truncate_for_llm(docs_input, limits["max_chars"]),
    )
    timings["analyze_docs"] = _done(t0)
    (output_dir / "docs_summary.md").write_text(docs_summary, encoding="utf-8")

    # 5. 并发目录分析
    grouped_files = _group_files_by_directory(relevant_files, limits["dirs"], limits["files_per_dir"])
    max_workers = min(4, max(1, len(grouped_files)))

    t0 = _step(f"并发分析 {len(grouped_files)} 个目录 (workers={max_workers})...")
    dir_summaries_dir = output_dir / "directory_summaries"
    dir_summaries_dir.mkdir(exist_ok=True)

    directory_summaries: list[dict] = []
    directory_read_files: list[dict] = []
    directory_failed_files: list[dict] = []

    def _worker(directory: str, file_paths: list[str]) -> dict:
        w_client = GitLabClient(base_url=base_url, private_token=token)
        dir_code, dir_read, dir_failed = _collect_code_content_with_evidence(
            w_client, project_path, file_paths,
            ref=branch, max_file_size_kb=gitlab_config.max_file_size_kb,
        )
        summary = analyzer.analyze_directory(
            directory,
            _truncate_for_llm(docs_summary, 20_000),
            _truncate_for_llm(dir_code, limits["max_chars"]),
        )
        return {
            "directory": directory,
            "files": file_paths,
            "summary": summary,
            "read_files": dir_read,
            "failed_files": dir_failed,
        }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_worker, directory, file_paths): directory
            for directory, file_paths in grouped_files.items()
        }
        for idx, future in enumerate(as_completed(futures), start=1):
            directory = futures[future]
            try:
                item = future.result()
            except Exception as e:
                logger.warning("目录 {} 分析失败: {}", directory, e)
                item = {
                    "directory": directory,
                    "files": grouped_files[directory],
                    "summary": f"分析失败：{e}",
                    "read_files": [],
                    "failed_files": [{"path": directory, "error": str(e)}],
                }
            directory_summaries.append(item)
            directory_read_files.extend(item["read_files"])
            directory_failed_files.extend(item["failed_files"])

            safe_name = directory.replace("/", "_").replace("\\", "_")
            (dir_summaries_dir / f"{safe_name}.md").write_text(item["summary"], encoding="utf-8")
            print(f"  [{idx}/{len(grouped_files)}] {directory} "
                  f"({len(item['read_files'])} files read, {len(item['failed_files'])} failed)")

    directory_summaries.sort(key=lambda x: (0 if x["directory"] == ROOT_DIR else 1, x["directory"]))
    timings["analyze_directories"] = _done(t0)

    # 6. 综合分析
    directory_summary_text, _ = _build_directory_digest(
        directory_summaries,
        per_dir_chars=DIRECTORY_ARTIFACT_SNIPPET_CHARS,
        max_chars=DIRECTORY_ARTIFACT_MAX_CHARS,
        include_files=True,
    )
    directory_synthesis_text, _ = _build_directory_digest(
        directory_summaries,
        per_dir_chars=DIRECTORY_SYNTHESIS_SNIPPET_CHARS,
        max_chars=DIRECTORY_SYNTHESIS_INPUT_MAX_CHARS,
        include_files=False,
    )
    (output_dir / "directory_summary_map.md").write_text(directory_summary_text, encoding="utf-8")

    t0 = _step("LLM 综合分析...")
    synthesis_input_tree = _truncate_for_llm(file_tree_text, 20_000)
    synthesis_input_docs = _truncate_for_llm(docs_summary, 30_000)
    comprehensive_summary = analyzer.synthesize_project_analysis(
        synthesis_input_tree,
        synthesis_input_docs,
        directory_synthesis_text,
    )
    comprehensive_summary, _ = _truncate_text(
        comprehensive_summary,
        max_chars=ARTIFACT_CONTENT_MAX_CHARS,
        suffix="\n\n...（综合分析已压缩为可审核上下文）",
    )
    timings["synthesize"] = _done(t0)
    (output_dir / "project_analysis.md").write_text(comprehensive_summary, encoding="utf-8")

    # 7. API 契约
    api_related_files = [
        p for p in relevant_files
        if any(kw in p.lower() for kw in ["router", "route", "controller", "handler", "api"])
    ][:max(20, limits["files_per_dir"])]

    if api_related_files:
        t0 = _step(f"LLM 分析 {len(api_related_files)} 个 API 文件...")
        api_code, read_api_files, failed_api_files = _collect_code_content_with_evidence(
            client, project_path, api_related_files,
            ref=branch, max_file_size_kb=gitlab_config.max_file_size_kb,
        )
        contracts = analyzer.analyze_api_contracts(_truncate_for_llm(api_code, limits["max_chars"]))
        timings["api_contracts"] = _done(t0)
    else:
        read_api_files = []
        failed_api_files = []
        contracts = "未在本次筛选文件中识别到明确的 API 入口文件。"
        print(f"\n{YELLOW}⚠{RESET} 未找到 API 相关文件，跳过 API 契约分析")

    (output_dir / "api_contracts.md").write_text(contracts, encoding="utf-8")

    # 8. 写 evidence 和 stats
    all_read = read_doc_files + directory_read_files + read_api_files
    all_failed = failed_doc_files + directory_failed_files + failed_api_files

    evidence = {
        "tree_files_total": len(tree_files),
        "doc_files": doc_files,
        "read_doc_files": read_doc_files,
        "failed_doc_files": failed_doc_files,
        "directories": [
            {"directory": item["directory"], "files": item["files"]}
            for item in directory_summaries
        ],
        "read_code_files": directory_read_files,
        "failed_code_files": directory_failed_files,
        "api_related_files": api_related_files,
        "read_api_files": read_api_files,
        "failed_api_files": failed_api_files,
    }
    (output_dir / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    total_time = sum(timings.values())
    stats = {
        "repo_url": repo_url,
        "branch": branch,
        "analysis_level": level,
        "llm_model": llm_config.model,
        "files_in_tree": len(tree_files),
        "files_selected": len(relevant_files),
        "files_read": len(all_read),
        "files_failed": len(all_failed),
        "docs_selected": len(doc_files),
        "directories_analyzed": len(directory_summaries),
        "api_files_found": len(api_related_files),
        "coverage_pct": round(len(relevant_files) / max(len(tree_files), 1) * 100, 1),
        "read_coverage_pct": round(len(all_read) / max(len(tree_files), 1) * 100, 1),
        "timings": {k: round(v, 2) for k, v in timings.items()},
        "total_time_sec": round(total_time, 2),
    }
    (output_dir / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # 打印摘要
    print(f"\n{'='*60}")
    print(f"  {GREEN}✓ 分析完成{RESET}")
    print(f"  文件树:        {len(tree_files)} files")
    print(f"  代码筛选:      {len(relevant_files)} files ({stats['coverage_pct']}%)")
    print(f"  实际读取:      {len(all_read)} files ({stats['read_coverage_pct']}%)")
    print(f"  读取失败:      {len(all_failed)} files")
    print(f"  目录分析:      {len(directory_summaries)} directories")
    print(f"  API 文件:      {len(api_related_files)} files")
    print(f"  总耗时:        {total_time:.1f}s")
    for name, sec in timings.items():
        print(f"    {name}: {sec:.1f}s")
    print(f"  输出目录:      {output_dir}")
    print(f"{'='*60}")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Viktor 接入分析独立测试：输入 Git repo URL，输出结构化分析结果",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo-url", required=True, help="GitLab 仓库 URL")
    parser.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help="分支名；省略时调用 GitLab API 使用项目的 default_branch",
    )
    parser.add_argument("--level", default="standard", choices=["quick", "standard", "deep"],
                        help="分析档位 (默认 standard)")
    parser.add_argument("--output-dir", default=None,
                        help="输出目录 (默认 ./analysis_output/<repo-slug>/)")
    parser.add_argument("--token", default=None,
                        help="GitLab Private Token (默认从 config.yaml / 环境变量读取)")
    parser.add_argument("--description", default="",
                        help="项目描述先验信息，注入到 LLM Prompt 中提升分析质量")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING",
               format="<dim>{time:HH:mm:ss}</dim> <level>{level: <5}</level> {message}")

    token = (args.token or gitlab_config.token_for_repo_url(args.repo_url) or "").strip()
    if not token:
        print(f"{RED}✗ 未配置 GitLab Token。"
              f"请通过 --token 参数或 gitlab.credentials 对应环境变量提供。{RESET}")
        return 1

    if not (llm_config.api_key or "").strip():
        print(f"{RED}✗ 未配置 LLM API Key。"
              f"请通过 DEEPSEEK_API_KEY 环境变量或 config.yaml 配置。{RESET}")
        return 1

    slug = GitLabClient.extract_project_path(args.repo_url).replace("/", "-").replace(".", "-")
    output_dir = Path(args.output_dir) if args.output_dir else Path(f"./analysis_output/{slug}")

    base_url = gitlab_config.resolve_base_url(args.repo_url)
    gl_preview = GitLabClient(base_url=base_url, private_token=token)
    project_path = GitLabClient.extract_project_path(args.repo_url)
    branch = args.branch
    if not branch:
        try:
            meta = gl_preview.get_project(project_path)
            branch = (meta.get("default_branch") or "master").strip() or "master"
            print(f"{DIM}未指定 --branch，使用 GitLab default_branch: {branch}{RESET}")
        except Exception as e:
            print(f"{RED}✗ 无法解析默认分支（请检查 Token、项目路径与网络）: {e}{RESET}")
            return 1

    try:
        stats = run_analysis(
            repo_url=args.repo_url,
            branch=branch,
            level=args.level,
            output_dir=output_dir,
            token=token,
            description=args.description,
        )
        if "error" in stats:
            return 1
        return 0
    except Exception as e:
        print(f"\n{RED}✗ 分析失败: {e}{RESET}")
        logger.exception("分析失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
