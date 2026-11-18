"""Set up one shot without duplicating owned audio nodes."""
from pathlib import Path
from typing import Optional
import uuid

from cinesync.core.engine import ValidationError, safe_path, validate_fps


def playback_slider() -> Optional[str]:
    """Return a real interactive Time Slider, never requiring UI in mayapy."""
    import maya.cmds as cmds

    if cmds.about(batch=True):
        return None
    import maya.mel as mel
    slider = mel.eval('global string $gPlayBackSlider; '
                      '$cineSyncPlaybackSlider = $gPlayBackSlider;')
    return slider if slider and cmds.timeControl(slider, exists=True) else None


def mark_audio_owned(node: str) -> None:
    """Add the ownership flag to a resolved DG object, independent of selection."""
    import maya.api.OpenMaya as om

    try:
        selection = om.MSelectionList()
        selection.add(node)
        function = om.MFnDependencyNode(selection.getDependNode(0))
        if function.typeName != 'audio':
            raise ValueError(f'Expected an audio node, resolved {function.typeName}')
        if not function.hasAttribute('cineSyncOwned'):
            attribute = om.MFnNumericAttribute().create(
                'cineSyncOwned', 'cso', om.MFnNumericData.kBoolean, True)
            function.addAttribute(attribute)
        function.findPlug('cineSyncOwned', False).setBool(True)
    except Exception as exc:
        raise ValidationError(f'Cannot mark audio node {node!r} as CineSync-owned: {exc}') from exc


def create_audio(audio: Path, start: int, duration: int) -> str:
    """Create an independent audio DG node, even for an already loaded WAV."""
    import maya.cmds as cmds

    # Packaged WAV duration is already validated by the exporter. Use the
    # manifest's exact shot bounds; older Maya Python wave readers cannot
    # decode the WAVE_FORMAT_EXTENSIBLE headers common in 24-bit PCM exports.
    node = cmds.createNode('audio', name=':cineSyncAudio_' + uuid.uuid4().hex)
    cmds.setAttr(node + '.filename', str(audio), type='string')
    cmds.setAttr(node + '.offset', start)
    cmds.setAttr(node + '.sourceStart', 0)
    cmds.setAttr(node + '.sourceEnd', duration)
    cmds.setAttr(node + '.endFrame', start + duration)
    mark_audio_owned(node)
    return node


def ingest(manifest: dict, shot: dict, root: Path, confirm_fps: bool = True,
           replace_audio: bool = True) -> None:
    import maya.cmds as cmds

    fps = validate_fps(manifest["fps"])
    batch = cmds.about(batch=True)
    if confirm_fps:
        confirm_frame_rate(fps)
    audio = safe_path(root, shot["audio_track"]) if shot.get("audio_track") else None
    if audio and not audio.is_file():
        raise ValidationError(f"Missing audio: {audio}")
    cmds.undoInfo(openChunk=True, chunkName="CineSync ingest")
    try:
        units = {24: "film", 25: "pal", 30: "ntsc", 48: "show", 50: "palf", 60: "ntscf"}
        cmds.currentUnit(time=units.get(fps, f"{fps}fps"), updateAnimation=False)
        start = shot["maya_start_frame"]
        end = start + shot["duration_frames"] - 1
        cmds.playbackOptions(min=start, max=end, animationStartTime=start, animationEndTime=end)
        if replace_audio:
            for node in cmds.ls(type="audio") or []:
                if cmds.attributeQuery("cineSyncOwned", node=node, exists=True):
                    cmds.delete(node)
        if audio:
            node = create_audio(audio, start, shot['duration_frames'])
            if not batch:
                slider = playback_slider()
                if slider:
                    cmds.timeControl(slider, edit=True, sound=node, displaySound=True)
        cmds.currentTime(start)
    finally:
        cmds.undoInfo(closeChunk=True)


def confirm_frame_rate(fps: int) -> None:
    """Obtain consent before changing time units or staging any FBX nodes."""
    import maya.cmds as cmds
    import maya.api.OpenMaya as om

    current = om.MTime(1, om.MTime.uiUnit()).asUnits(om.MTime.kSeconds)
    if abs(1.0 / current - fps) <= 1e-8:
        return
    if cmds.about(batch=True):
        raise ValidationError('Frame rate change needs confirmation; use '
                              'confirm_fps=False for explicit batch scene setup')
    unit = cmds.currentUnit(query=True, time=True)
    answer = cmds.confirmDialog(title='CineSync frame rate',
                                message=f'Change scene from {unit} to {fps} fps?',
                                button=['Change', 'Cancel'], defaultButton='Cancel',
                                cancelButton='Cancel', dismissString='Cancel')
    if answer != 'Change':
        raise ValidationError('Scene setup cancelled')
