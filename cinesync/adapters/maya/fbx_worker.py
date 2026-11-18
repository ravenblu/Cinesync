"""mayapy entrypoint: inspect FBX Takes or export normalized shot skeletons.

Run only in a disposable Maya standalone process, never in an artist scene.
"""
from __future__ import annotations

import json
import sys
import traceback
from fractions import Fraction
from pathlib import Path

# This file is also executed directly from the bundled maya_runtime directory.
if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from cinesync.core.timecode import packed, rational, validate_timecode
from cinesync.core.engine import frames_to_smpte, smpte_to_frames


def native_fps() -> int:
    import maya.api.OpenMaya as om

    rate = 1.0 / om.MTime(1, om.MTime.uiUnit()).asUnits(om.MTime.kSeconds)
    rounded = round(rate)
    if rounded < 1 or abs(rate - rounded) > 1e-6:
        raise ValueError(f'FBX animation sampling rate is {rate:g} fps; expected a positive integer')
    return rounded


def embedded_timecode(nodes: list) -> dict:
    """Read the explicit v1 metadata contract; never infer TC from key times."""
    import maya.cmds as cmds

    fields = ('cineSyncSourceTimecode', 'cineSyncTimecodeFps',
              'cineSyncAnchorFrame', 'cineSyncTimecodeVersion')
    matches = [node for node in nodes if any(cmds.attributeQuery(
        field, node=node, exists=True) for field in fields)]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous embedded FBX shoot timecode; '
                         'this FBX metadata format is not supported')
    node = matches[0]
    if not all(cmds.attributeQuery(field, node=node, exists=True) for field in fields):
        raise ValueError('Incomplete embedded FBX shoot timecode')
    values = [cmds.getAttr(node + '.' + field) for field in fields]
    return validate_timecode(dict(value=values[0], fps=values[1],
                                  local_frame=values[2], version=values[3],
                                  origin='fbx:cinesync-v1:' + node))


def take_info(path: str) -> list[dict]:
    import maya.cmds as cmds
    import maya.mel as mel
    from cinesync.adapters.maya.fbx import import_file, settings, skeleton_nodes

    with settings():
        mel.eval('FBXRead -f ' + json.dumps(Path(path).as_posix(), ensure_ascii=False) + ';')
        count = int(mel.eval('FBXGetTakeCount;'))
        names = [str(mel.eval(f'FBXGetTakeName {index};'))
                 for index in range(1, count + 1)]
    if not names:
        raise ValueError('FBX contains no animation Takes')
    result = []
    for index, name in enumerate(names, 1):
        cmds.file(new=True, force=True)
        # Importing the selected Take also obtains its actual file frame rate;
        # no inference from an arbitrary scene's current time unit is used.
        import_file(Path(path), index, 'source', file_fps=True)
        nodes = skeleton_nodes()
        times = cmds.keyframe(nodes, query=True, timeChange=True) or []
        if not times:
            raise ValueError(f'Take {name!r} contains no keyed skeleton animation')
        first = Fraction(str(min(times))).limit_denominator(1000000)
        last = Fraction(str(max(times))).limit_denominator(1000000)
        try:
            sample_rate = native_fps()
            tc = embedded_timecode(nodes)
            if not first <= rational(tc['local_frame']) <= last:
                raise ValueError('Embedded timecode anchor is outside animation range')
        except ValueError as exc:
            raise ValueError(f'{path}, Take {name!r}: {exc}') from exc
        result.append({'timecode': tc, 'take_index': index, 'take_name': name,
                       'native_fps': sample_rate,
                       'first_frame': packed(first), 'last_frame': packed(last)})
    return result


def export_item(item: dict) -> None:
    import maya.cmds as cmds
    import maya.mel as mel
    from cinesync.adapters.maya.fbx import import_file, settings, skeleton_nodes

    source = item['source']
    source_file = Path(source['file'])
    stat = source_file.stat()
    if (stat.st_size, stat.st_mtime_ns) != (source['size'], source['mtime_ns']):
        raise ValueError('Source FBX changed after inspection')
    cmds.file(new=True, force=True)
    cmds.currentUnit(linear='cm')
    if cmds.upAxis(query=True, axis=True) != 'y':
        cmds.upAxis(axis='y')
    imported = import_file(Path(source['file']), source['take_index'],
                           'source', file_fps=True)
    if native_fps() != source['native_fps']:
        raise ValueError('FBX frame rate changed after inspection')
    nodes = skeleton_nodes()
    actual_tc = embedded_timecode(nodes)
    if any(actual_tc[key] != source['timecode'][key]
           for key in ('value', 'fps', 'local_frame', 'version')):
        raise ValueError('Embedded FBX timecode changed after inspection')
    fps = item['fps']
    cmds.currentUnit(time=f'{fps}fps', updateAnimation=True)
    start = Fraction(item['maya_start_frame'])
    end = start + item['duration_frames'] - 1
    source_start = rational(source['source_in_frame']) * fps / source['native_fps']
    curves = cmds.listConnections(nodes, source=True, destination=False,
                                   type='animCurve') or []
    if not curves:
        raise ValueError('Imported skeleton has no animation curves')
    cmds.keyframe(list(set(curves)), edit=True, relative=True,
                  timeChange=float(start - source_start))
    attributes = ['tx', 'ty', 'tz', 'rx', 'ry', 'rz', 'sx', 'sy', 'sz']
    cmds.bakeResults(nodes, time=(float(start), float(end)), sampleBy=1,
                     simulation=True, preserveOutsideKeys=False,
                     disableImplicitControl=True, attribute=attributes)
    curves = cmds.listConnections(nodes, source=True, destination=False,
                                   type='animCurve') or []
    if curves:
        curves = list(set(curves))
        cmds.cutKey(curves, time=(-1e15, float(start) - 1e-6), clear=True)
        cmds.cutKey(curves, time=(float(end) + 1e-6, 1e15), clear=True)
    # Clean only nodes created by this import. Maya's startup cameras belong
    # to the standalone scene and are non-deletable; selected export omits them.
    for shape in cmds.ls(imported, type='mesh', long=True) or []:
        cmds.delete(shape)
    remaining = [node for node in imported if cmds.objExists(node)]
    for node in sorted(cmds.ls(remaining, type='transform', long=True) or [],
                       key=lambda name: name.count('|'), reverse=True):
        if node not in nodes and cmds.objExists(node):
            cmds.delete(node)
    cmds.playbackOptions(min=float(start), max=float(end),
                         animationStartTime=float(start), animationEndTime=float(end))
    cmds.currentTime(float(start))
    # The published shot carries a new local anchor, not the source's frame zero.
    tc_node = next(node for node in nodes if cmds.attributeQuery(
        'cineSyncSourceTimecode', node=node, exists=True))
    delta = ((rational(source['source_in_frame']) - rational(actual_tc['local_frame']))
             * fps / source['native_fps'])
    if delta.denominator != 1:
        raise ValueError('Shot timecode does not land on an editorial frame')
    shot_tc = frames_to_smpte(smpte_to_frames(actual_tc['value'], fps) + int(delta), fps)
    cmds.setAttr(tc_node + '.cineSyncSourceTimecode', shot_tc, type='string')
    cmds.setAttr(tc_node + '.cineSyncAnchorFrame', float(start))
    cmds.select(nodes, replace=True)
    with settings():
        mel.eval('FBXResetExport;')
        mel.eval('FBXExportFileVersion -v "FBX202000";')
        mel.eval('FBXExportInputConnections -v false;')
        mel.eval('FBXExportSkins -v false;')
        mel.eval('FBXExportShapes -v false;')
        mel.eval('FBXExportCameras -v false;')
        mel.eval('FBXExportLights -v false;')
        mel.eval('FBXExportConstraints -v false;')
        mel.eval('FBXExport -f ' + json.dumps(Path(item['output']).as_posix(),
                                           ensure_ascii=False) + ' -s;')
    if not Path(item['output']).is_file():
        raise ValueError('FBX exporter did not create a shot file')
    stat = source_file.stat()
    if (stat.st_size, stat.st_mtime_ns) != (source['size'], source['mtime_ns']):
        raise ValueError('Source FBX changed during export')


def main() -> int:
    response = Path(sys.argv[2])
    initialized = False
    try:
        import maya.standalone
        maya.standalone.initialize(name='python')
        initialized = True
        request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
        if request['operation'] == 'inspect':
            result = {'takes': take_info(request['file'])}
        elif request['operation'] == 'export':
            for item in request['items']:
                export_item(item)
            result = {'exported': len(request['items'])}
        else:
            raise ValueError('Unknown FBX worker operation')
        response.write_text(json.dumps({'ok': True, 'result': result}), encoding='utf-8')
        return 0
    except Exception as exc:
        traceback.print_exc()
        response.write_text(json.dumps({'ok': False, 'error': str(exc)}), encoding='utf-8')
        return 1
    finally:
        if initialized:
            maya.standalone.uninitialize()


if __name__ == '__main__':
    raise SystemExit(main())
