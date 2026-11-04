"""Materialize a candidate cordis overlay for a dsh ``--patch`` invocation.

``cordis-plugin-include`` of a profile living outside ``dsh_home`` makes Node
resolve bare ``@deepseek-ai/*`` imports from the candidate directory, which
has no runtime ``node_modules``. Writing the candidate entries as a ``--patch``
file keeps package resolution on the harness installation fallback while
rewriting relative ``./plugins/*.mjs`` names to absolute paths so local plugins
still load.

The same rewrite serves two deployments: the in-container candidate runtime
(``plugin_root`` is the candidate directory as seen inside the task container)
and the legacy host layout used by ``mutate_local.py``.

Rewrites use a YAML compose/serialize round-trip (not regex) so quote styles,
insert blocks, and non-plugin ``name`` fields stay intact. Custom tags such as
``!!js`` are preserved by PyYAML compose/serialize.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _is_relative_plugin(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("./")


def _rewrite_mapping(node: yaml.Node, candidate_dir: Path, plugin_root: Path) -> None:
    if not isinstance(node, yaml.MappingNode):
        return
    for i, (key_node, value_node) in enumerate(node.value):
        if (
            isinstance(key_node, yaml.ScalarNode)
            and key_node.value == "name"
            and isinstance(value_node, yaml.ScalarNode)
            and _is_relative_plugin(value_node.value)
        ):
            # ``plugin_root`` may be a container-side path that does not exist
            # on this host, so do not call resolve(): keep the path lexical.
            absolute = str(plugin_root / value_node.value[2:])
            # Prefer double quotes so Windows and POSIX paths dump stably.
            node.value[i] = (
                key_node,
                yaml.ScalarNode(
                    tag=value_node.tag,
                    value=absolute,
                    style='"',
                ),
            )
        else:
            _rewrite_node(value_node, candidate_dir, plugin_root)


def _rewrite_node(node: yaml.Node, candidate_dir: Path, plugin_root: Path) -> None:
    if isinstance(node, yaml.MappingNode):
        _rewrite_mapping(node, candidate_dir, plugin_root)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _rewrite_node(child, candidate_dir, plugin_root)


def rewrite_relative_plugin_names(
    text: str,
    candidate_dir: Path,
    *,
    plugin_root: Path | None = None,
) -> str:
    """Turn relative plugin ``name`` fields into absolute paths.

    ``plugin_root`` defaults to ``candidate_dir``. Pass the candidate's path
    inside the task container when the overlay is consumed there.
    """
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        # Fall back to a line-oriented rewrite only if the document is not YAML;
        # valid cordis profiles always parse.
        raise
    if root is None:
        return text
    _rewrite_node(root, candidate_dir, plugin_root if plugin_root is not None else candidate_dir)
    dumped = yaml.serialize(root)
    # serialize() may omit a trailing newline; keep files newline-terminated.
    return dumped if dumped.endswith("\n") else dumped + "\n"


def materialize_candidate_overlay(
    candidate_dir: Path,
    dsh_home: Path,
    *,
    source_name: str = "profile.cordis.yml",
    output_name: str = "candidate.overlay.cordis.yml",
    plugin_root: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    """Write the candidate profile as a patch with absolute plugin paths."""
    source = candidate_dir / source_name
    if not source.is_file():
        raise FileNotFoundError(f"candidate profile missing: {source}")
    output = output_path if output_path is not None else dsh_home / output_name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rewrite_relative_plugin_names(source.read_text(), candidate_dir, plugin_root=plugin_root))
    return output
