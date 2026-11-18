"""Small FBX plug-in boundary shared by the batch worker and Maya ingest."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def settings() -> Iterator[None]:
    import maya.cmds as cmds
    import maya.mel as mel

    cmds.loadPlugin('fbxmaya', quiet=True)
    mel.eval('FBXPushSettings;')
    try:
        yield
    finally:
        mel.eval('FBXPopSettings;')


def import_file(path: Path, take: int, namespace: str,
                file_fps: bool = False) -> list[str]:
    import maya.cmds as cmds
    import maya.mel as mel

    with settings():
        mel.eval('FBXResetImport;')
        mel.eval('FBXImportMode -v "add";')
        mel.eval('FBXImportCameras -v false;')
        mel.eval('FBXImportLights -v false;')
        mel.eval('FBXImportConstraints -v false;')
        mel.eval('FBXImportSkins -v false;')
        mel.eval('FBXImportShapes -v false;')
        mel.eval('FBXImportFillTimeline -v false;')
        mel.eval('FBXImportResamplingRateSource -v "File";')
        mel.eval('FBXImportMergeAnimationLayers -v true;')
        mel.eval(f'FBXImportSetMayaFrameRate -v {str(file_fps).lower()};')
        mel.eval(f'FBXImportSetTake -takeIndex {int(take)};')
        return cmds.file(str(path), i=True, type='FBX', namespace=namespace,
                         mergeNamespacesOnClash=False, returnNewNodes=True,
                         options='v=0;', prompt=False, executeScriptNodes=False,
                         importFrameRate=file_fps, importTimeRange='keep') or []


def skeleton_nodes() -> list[str]:
    """Include animated transform ancestors; retain the captured root motion."""
    import maya.cmds as cmds

    nodes = set(cmds.ls(type='joint', long=True) or [])
    if not nodes:
        raise ValueError('FBX contains no joints; a solved skeleton animation is required')
    for joint in list(nodes):
        parents = cmds.listRelatives(joint, parent=True, fullPath=True) or []
        while parents:
            nodes.add(parents[0])
            parents = cmds.listRelatives(parents[0], parent=True, fullPath=True) or []
    return sorted(nodes)
