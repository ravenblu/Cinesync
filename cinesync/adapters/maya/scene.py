"""Shared scene transaction for the desktop receiver and package launcher."""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def open_shot(manifest: dict, shot: dict, root: Path,
              show_player: Callable[[], object], confirm_fps: bool = True) -> None:
    import maya.cmds as cmds
    from .ingest import confirm_frame_rate, ingest, playback_slider
    from .mocap import MocapImport
    from cinesync.core.engine import validate_fps

    if not cmds.undoInfo(query=True, state=True):
        raise RuntimeError('Enable Maya Undo before receiving a shot to allow rollback')
    fps = validate_fps(manifest['fps'])
    if confirm_fps:
        confirm_frame_rate(fps)
    previous_unit = cmds.currentUnit(query=True, time=True)
    previous_frame = cmds.currentTime(query=True)
    previous_selection = cmds.ls(selection=True, long=True) or []
    previous_range = {key: cmds.playbackOptions(query=True, **{key: True})
                      for key in ('min', 'max', 'animationStartTime', 'animationEndTime')}
    audio_before = set(cmds.ls(type='audio', uuid=True) or [])
    owned_audio = [key for node in (cmds.ls(type='audio') or [])
                   if cmds.attributeQuery('cineSyncOwned', node=node, exists=True)
                   for key in (cmds.ls(node, uuid=True) or [])]
    slider = playback_slider()
    slider_state = None
    if slider:
        slider_state = (cmds.timeControl(slider, query=True, sound=True),
                        cmds.timeControl(slider, query=True, displaySound=True))
    transaction = MocapImport()
    error = None
    player = None
    chunk_open = False
    try:
        # The FBX plug-in may flush Maya's Undo queue. Import only new nodes
        # before starting the reversible scene-setup chunk, and track them
        # explicitly. No marker or old scene data is changed before import.
        # Match units before import so normalized FBX keys stay on 1001,
        # without retiming preexisting artist animation.
        cmds.currentUnit(time=f'{fps}fps', updateAnimation=False)
        transaction.stage(shot, root)
        cmds.undoInfo(openChunk=True, chunkName='CineSync receive')
        chunk_open = True
        # Keep old audio alive until every fallible setup/player check passes.
        ingest(manifest, shot, root, confirm_fps=False, replace_audio=False)
        if shot.get('audio_track'):
            audio_after = set(cmds.ls(type='audio', uuid=True) or [])
            if not audio_after - audio_before:
                raise RuntimeError('Audio setup did not create a distinct new node; '
                                   'old audio will not be deleted')
        # FBX must not take ownership of the shot range or current frame.
        cmds.currentTime(shot['maya_start_frame'])
        player = show_player()
        transaction.commit()
        previous_audio = [name for key in owned_audio for name in (cmds.ls(key) or [])]
        if previous_audio:
            cmds.delete(previous_audio)
    except Exception as exc:
        error = exc
    finally:
        if chunk_open:
            cmds.undoInfo(closeChunk=True)
    if error:
        problems = []
        if player is not None and hasattr(player, 'close'):
            try:
                player.close()
            except Exception as exc:
                problems.append(str(exc))
        # Never undo an empty queue or an unrelated artist command. Explicit
        # cleanup/restoration below works even when FBX discarded its Undo.
        if (chunk_open and not cmds.undoInfo(query=True, undoQueueEmpty=True)
                and cmds.undoInfo(query=True, undoName=True) == 'CineSync receive'):
            try:
                cmds.undo()
            except Exception as exc:
                problems.append(str(exc))

        def restore(label, action):
            try:
                action()
            except Exception as exc:
                problems.append(f'{label}: {exc}')

        restore('new FBX nodes', transaction.rollback)
        new_audio = set(cmds.ls(type='audio', uuid=True) or []) - audio_before
        new_audio_names = [name for key in new_audio for name in (cmds.ls(key) or [])]
        if new_audio_names:
            restore('new audio nodes', lambda: cmds.delete(new_audio_names))
        restore('frame rate', lambda: cmds.currentUnit(time=previous_unit, updateAnimation=False))
        restore('playback range', lambda: cmds.playbackOptions(**previous_range))
        restore('current frame', lambda: cmds.currentTime(previous_frame))
        if slider_state:
            sound, display = slider_state
            restore('Time Slider', lambda: cmds.timeControl(
                slider, edit=True, sound=sound if sound and cmds.objExists(sound) else '',
                displaySound=display))
        existing = [node for node in previous_selection if cmds.objExists(node)]
        restore('selection', lambda: cmds.select(existing, replace=True) if existing
                else cmds.select(clear=True))
        missing_old = [key for key in transaction.previous_nodes.union(owned_audio)
                       if not cmds.ls(key)]
        if missing_old:
            problems.append(f'Previous owned data could not be restored: {missing_old}')
        suffix = '; rollback incomplete: ' + '; '.join(problems) if problems else '; scene settings restored'
        raise RuntimeError(str(error) + suffix) from error
    existing = [node for node in previous_selection if cmds.objExists(node)]
    cmds.select(existing, replace=True) if existing else cmds.select(clear=True)
