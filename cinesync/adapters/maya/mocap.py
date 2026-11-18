"""Track imported FBX nodes explicitly; never merge into artist-owned rigs."""
from __future__ import annotations

import uuid
from pathlib import Path

from cinesync.core.engine import safe_path


class MocapImport:
    def __init__(self) -> None:
        import maya.cmds as cmds

        self.created: set[str] = set()
        self.namespace = ''
        # Snapshot old owners explicitly. A general ls(uuid=True) scene diff
        # can omit internal/network nodes and must not decide who is "old".
        self.previous_owners = [key for node in self.owned_sessions()
                                for key in (cmds.ls(node, uuid=True) or [])]
        self.previous_nodes = set(self.previous_owners)
        for key in self.previous_owners:
            owner = cmds.ls(key, long=True)[0]
            namespace = cmds.getAttr(owner + '.cineSyncMocapNamespace')
            members = cmds.listConnections(owner + '.cineSyncMocapNodes',
                                            source=True, destination=False) or []
            members = [node for node in members if self.in_namespace(node, namespace)]
            if members:
                self.previous_nodes.update(cmds.ls(members, uuid=True) or [])

    @staticmethod
    def scene_ids() -> set[str]:
        import maya.cmds as cmds

        return set(cmds.ls('::*', dependencyNodes=True, uuid=True) or [])

    @staticmethod
    def owned_sessions() -> list[str]:
        import maya.cmds as cmds

        return [node for node in (cmds.ls('::*', type='network', long=True) or [])
                if cmds.attributeQuery('cineSyncMocapOwned', node=node, exists=True)
                and cmds.getAttr(node + '.cineSyncMocapOwned')]

    @staticmethod
    def in_namespace(name: str, namespace: str) -> bool:
        leaf = name.rsplit('|', 1)[-1].lstrip(':')
        return bool(namespace) and leaf.startswith(namespace.lstrip(':') + ':')

    def imported_ids(self, before: set[str]) -> set[str]:
        """Own only this import namespace, never global Maya managers."""
        import maya.cmds as cmds

        return {key for key in self.scene_ids() - before
                if any(self.in_namespace(name, self.namespace)
                       for name in (cmds.ls(key, long=True) or []))}

    def stage(self, shot: dict, root: Path) -> None:
        import maya.cmds as cmds
        from .fbx import import_file

        entries = shot.get('mocap', [])
        if not entries:
            return
        entry = entries[0]
        self.namespace = f"cinesync_{shot['shot_id']}_body_{uuid.uuid4().hex[:8]}"
        before = self.scene_ids()
        try:
            import_file(safe_path(root, entry['file']), 1, self.namespace)
            imported = self.imported_ids(before)
            names = [name for key in imported for name in (cmds.ls(key, long=True) or [])]
            if not any(cmds.nodeType(name) == 'joint' for name in names):
                raise ValueError('Shot FBX contains no skeleton')
            owner = cmds.createNode('network', name=':cineSyncMocapSession#')
            self.created.update(cmds.ls(owner, uuid=True) or [])
            cmds.addAttr(owner, longName='cineSyncMocapOwned', attributeType='bool')
            cmds.setAttr(owner + '.cineSyncMocapOwned', True)
            cmds.addAttr(owner, longName='cineSyncMocapNodes', attributeType='message', multi=True)
            cmds.addAttr(owner, longName='cineSyncMocapNamespace', dataType='string')
            cmds.setAttr(owner + '.cineSyncMocapNamespace', self.namespace, type='string')
            for index, name in enumerate(names):
                cmds.connectAttr(name + '.message', f'{owner}.cineSyncMocapNodes[{index}]')
        finally:
            self.created.update(self.imported_ids(before))

    def commit(self) -> None:
        import maya.cmds as cmds

        previous = []
        for key in self.previous_owners:
            owners = cmds.ls(key, long=True) or []
            if not owners:
                continue
            owner = owners[0]
            namespace = cmds.getAttr(owner + '.cineSyncMocapNamespace')
            nodes = cmds.listConnections(owner + '.cineSyncMocapNodes',
                                          source=True, destination=False) or []
            # Older sessions may have accidentally linked global track managers.
            # Preserve those nodes even when reading such ownership metadata.
            existing = [node for node in nodes if cmds.objExists(node)
                        and self.in_namespace(node, namespace)]
            owned_ids = {key for node in existing for key in (cmds.ls(node, uuid=True) or [])}
            for node in existing:
                if not cmds.objectType(node, isAType='dagNode'):
                    continue
                children = cmds.listRelatives(node, allDescendents=True, fullPath=True) or []
                for child in children:
                    if not set(cmds.ls(child, uuid=True) or []).issubset(owned_ids):
                        raise RuntimeError('An artist-owned node is parented under CineSync mocap. '
                                           'Move it out before replacing the skeleton.')
            previous.append((owner, namespace, existing))
        for owner, namespace, existing in previous:
            if existing:
                cmds.delete(existing)
            if cmds.objExists(owner):
                cmds.delete(owner)
            self.remove_empty_namespace(namespace)

    @staticmethod
    def remove_empty_namespace(namespace: str) -> None:
        import maya.cmds as cmds

        if namespace and cmds.namespace(exists=namespace):
            # Removing without merge/deleteContent fails safely if artists added nodes.
            children = cmds.namespaceInfo(namespace, listOnlyNamespaces=True,
                                           recurse=True, absoluteName=True) or []
            for name in sorted(children, key=lambda value: value.count(':'), reverse=True) + [namespace]:
                try:
                    cmds.namespace(removeNamespace=name)
                except RuntimeError:
                    pass

    def rollback(self) -> None:
        import maya.cmds as cmds

        nodes = [name for key in self.created for name in (cmds.ls(key, long=True) or [])]
        if nodes:
            cmds.delete(nodes)
        self.remove_empty_namespace(self.namespace)
