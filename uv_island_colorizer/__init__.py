import bpy
import bmesh
import random
import colorsys
import os
from bpy.props import (
    PointerProperty, BoolProperty, StringProperty,
    IntProperty, CollectionProperty,
)
from bpy.types import Panel, Operator, PropertyGroup, AddonPreferences, UIList


# =============================================================================
# ADDON PREFERENCES · persistent across restarts (stored in userpref.blend)
# =============================================================================

class UVShellColorizerPreferences(AddonPreferences):
    bl_idname = __package__

    bake_diffuse: BoolProperty(
        name="Bake Diffuse Map",
        description=(
            "After colorizing UV islands, bake the vertex colors to a diffuse PNG "
            "using Cycles (Blender equivalent of Maya's Transfer Maps / Surface Sampler)"
        ),
        default=False,
    )
    bake_resolution: IntProperty(
        name="Resolution",
        description="Bake texture resolution in pixels (square)",
        default=1024, min=64, max=8192,
    )
    bake_output_dir: StringProperty(
        name="Output Folder",
        description="Where to save baked PNGs. Blank = next to .blend file",
        default="", subtype='DIR_PATH',
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Saved across Blender restarts:")
        layout.prop(self, "bake_diffuse")
        col = layout.column()
        col.enabled = self.bake_diffuse
        col.prop(self, "bake_resolution")
        col.prop(self, "bake_output_dir")


# =============================================================================
# PER-SCENE DATA
# =============================================================================

class MeshListItem(PropertyGroup):
    """One entry in the multi-mesh target list."""
    obj: PointerProperty(
        name="Mesh Object",
        type=bpy.types.Object,
    )


class UVShellColorizerProps(PropertyGroup):
    # The multi-mesh list
    mesh_list: CollectionProperty(type=MeshListItem)
    mesh_list_index: IntProperty(name="Active index", default=0)

    # Picker: search field (name of object to add)
    picker_object: PointerProperty(
        name="Add Mesh",
        description="Pick a mesh from the scene or any collection to add to the list",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )


# =============================================================================
# UI LIST
# =============================================================================

class UVSHELL_UL_mesh_list(UIList):
    bl_idname = "UVSHELL_UL_mesh_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = item.obj
        if obj is None:
            layout.label(text="(missing)", icon='ERROR')
            return

        row = layout.row(align=True)

        # Object icon + name
        row.label(text="", icon='OUTLINER_OB_MESH')
        row.label(text=obj.name)

        # UV map count badge
        uv_count = len(obj.data.uv_layers)
        sub = row.row()
        sub.enabled = False
        sub.label(text=f"UVs: {uv_count}")

        # Already colorized badge
        if "UV Island Colors" in obj.data.color_attributes:
            row.label(text="", icon='CHECKMARK')


# =============================================================================
# OPERATORS · list management
# =============================================================================

class UVSHELL_OT_add_mesh(Operator):
    bl_idname = "uvshell.add_mesh"
    bl_label = "Add to List"
    bl_description = "Add the picked mesh to the target list"

    def execute(self, context):
        props = context.scene.uv_shell_colorizer
        obj = props.picker_object

        if obj is None:
            self.report({'WARNING'}, "No mesh selected in the picker.")
            return {'CANCELLED'}

        # Prevent duplicates
        for item in props.mesh_list:
            if item.obj == obj:
                self.report({'WARNING'}, f"'{obj.name}' is already in the list.")
                return {'CANCELLED'}

        entry = props.mesh_list.add()
        entry.obj = obj
        props.mesh_list_index = len(props.mesh_list) - 1

        # Clear picker field for the next pick
        props.picker_object = None
        return {'FINISHED'}


class UVSHELL_OT_add_selected(Operator):
    bl_idname = "uvshell.add_selected"
    bl_label = "Add Selected Objects"
    bl_description = "Add all currently selected mesh objects in the viewport to the list"

    def execute(self, context):
        props = context.scene.uv_shell_colorizer
        existing = {item.obj for item in props.mesh_list if item.obj}
        added = 0

        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            if obj in existing:
                continue
            entry = props.mesh_list.add()
            entry.obj = obj
            existing.add(obj)
            added += 1

        if added:
            props.mesh_list_index = len(props.mesh_list) - 1
            self.report({'INFO'}, f"Added {added} object(s).")
        else:
            self.report({'WARNING'}, "No new mesh objects were selected.")

        return {'FINISHED'}


class UVSHELL_OT_add_collection(Operator):
    """Operator that lists all collections as an enum and adds all their meshes."""
    bl_idname = "uvshell.add_collection"
    bl_label = "Add Collection"
    bl_description = "Add all mesh objects from a chosen collection to the list"

    # Dynamically populate from all collections in the file
    def collection_items(self, context):
        items = []
        for i, col in enumerate(bpy.data.collections):
            items.append((col.name, col.name, f"Collection: {col.name}", 'OUTLINER_COLLECTION', i))
        if not items:
            items.append(('__NONE__', "(No collections)", "", 'ERROR', 0))
        return items

    collection_name: bpy.props.EnumProperty(
        name="Collection",
        description="Choose a collection to add",
        items=collection_items,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        self.layout.prop(self, "collection_name", text="Collection")

    def execute(self, context):
        if self.collection_name == '__NONE__':
            self.report({'WARNING'}, "No collections found in the file.")
            return {'CANCELLED'}

        col = bpy.data.collections.get(self.collection_name)
        if col is None:
            self.report({'ERROR'}, f"Collection '{self.collection_name}' not found.")
            return {'CANCELLED'}

        props = context.scene.uv_shell_colorizer
        existing = {item.obj for item in props.mesh_list if item.obj}
        added = 0

        # Walk collection and all nested children recursively
        def collect_meshes(collection):
            nonlocal added
            for obj in collection.objects:
                if obj.type == 'MESH' and obj not in existing:
                    entry = props.mesh_list.add()
                    entry.obj = obj
                    existing.add(obj)
                    added += 1
            for child in collection.children:
                collect_meshes(child)

        collect_meshes(col)

        if added:
            props.mesh_list_index = len(props.mesh_list) - 1
            self.report({'INFO'}, f"Added {added} mesh(es) from '{col.name}'.")
        else:
            self.report({'WARNING'}, f"No new meshes found in '{col.name}'.")

        return {'FINISHED'}


class UVSHELL_OT_remove_mesh(Operator):
    bl_idname = "uvshell.remove_mesh"
    bl_label = "Remove"
    bl_description = "Remove the highlighted mesh from the list"

    def execute(self, context):
        props = context.scene.uv_shell_colorizer
        idx = props.mesh_list_index
        if 0 <= idx < len(props.mesh_list):
            props.mesh_list.remove(idx)
            props.mesh_list_index = max(0, idx - 1)
        return {'FINISHED'}


class UVSHELL_OT_clear_list(Operator):
    bl_idname = "uvshell.clear_list"
    bl_label = "Clear All"
    bl_description = "Remove all meshes from the list"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.uv_shell_colorizer
        props.mesh_list.clear()
        props.mesh_list_index = 0
        return {'FINISHED'}


# =============================================================================
# CORE HELPERS
# =============================================================================

def get_uv_shells(bm, uv_layer):
    """Union-Find over UV-edge adjacency → list of face-index sets per shell."""
    def uv_key(loop):
        uv = loop[uv_layer].uv
        return (round(uv.x, 5), round(uv.y, 5))

    face_count = len(bm.faces)
    parent = list(range(face_count))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    uv_edge_to_faces = {}
    for face in bm.faces:
        loops = face.loops
        n = len(loops)
        for i in range(n):
            key = frozenset([uv_key(loops[i]), uv_key(loops[(i + 1) % n])])
            uv_edge_to_faces.setdefault(key, []).append(face.index)

    for face_list in uv_edge_to_faces.values():
        for i in range(1, len(face_list)):
            union(face_list[0], face_list[i])

    shells = {}
    for face in bm.faces:
        shells.setdefault(find(face.index), set()).add(face.index)
    return list(shells.values())


def generate_distinct_colors(n):
    """N visually distinct RGBA colors via golden-ratio hue stepping."""
    colors, golden, hue = [], 0.618033988749895, random.random()
    for _ in range(n):
        hue = (hue + golden) % 1.0
        r, g, b = colorsys.hls_to_rgb(hue, random.uniform(0.45, 0.75), random.uniform(0.65, 1.0))
        colors.append((r, g, b, 1.0))
    return colors


def colorize_single_object(obj, report):
    """Paint UV-shell vertex colors on one object. Returns (success, num_shells)."""
    COLOR_LAYER_NAME = "UV Island Colors"
    mesh = obj.data

    if not mesh.uv_layers:
        report({'WARNING'}, f"'{obj.name}' skipped · no UV map.")
        return False, 0

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()

    uv_layer = bm.loops.layers.uv.active
    shells = get_uv_shells(bm, uv_layer)
    num_shells = len(shells)

    if num_shells == 0:
        report({'WARNING'}, f"'{obj.name}' skipped · no UV islands detected.")
        bm.free()
        return False, 0

    color_layer = bm.loops.layers.color.get(COLOR_LAYER_NAME) or \
                  bm.loops.layers.color.new(COLOR_LAYER_NAME)

    colors = generate_distinct_colors(num_shells)
    for idx, face_indices in enumerate(shells):
        c = colors[idx]
        for fi in face_indices:
            for loop in bm.faces[fi].loops:
                loop[color_layer][:] = c

    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    if COLOR_LAYER_NAME in mesh.color_attributes:
        mesh.color_attributes.active_color = mesh.color_attributes[COLOR_LAYER_NAME]

    return True, num_shells


def resolve_output_path(obj_name, prefs):
    folder = prefs.bake_output_dir.strip()
    if folder:
        folder = bpy.path.abspath(folder)
    else:
        blend_path = bpy.data.filepath
        folder = os.path.dirname(bpy.path.abspath(blend_path)) if blend_path else os.path.expanduser("~")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{obj_name}_diffuse_bake.png")


# =============================================================================
# BAKE PIPELINE
# =============================================================================

def bake_diffuse_from_vertex_colors(context, obj, prefs, report):
    """
    Bake UV Shell vertex colors → PNG (Cycles diffuse / color pass only).
    """
    COLOR_LAYER_NAME = "UV Island Colors"
    res = prefs.bake_resolution

    prev_engine = context.scene.render.engine
    context.scene.render.engine = 'CYCLES'

    prev_active = context.view_layer.objects.active
    prev_selected = list(context.selected_objects)
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    context.view_layer.objects.active = obj

    mesh = obj.data

    if not mesh.uv_layers:
        report({'ERROR'}, f"'{obj.name}' has no UV map · bake skipped.")
        context.scene.render.engine = prev_engine
        return False

    # Bake target image
    img_name = f"_bake_tmp_{obj.name}"
    if img_name in bpy.data.images:
        bpy.data.images.remove(bpy.data.images[img_name])
    bake_image = bpy.data.images.new(img_name, width=res, height=res, alpha=False)
    bake_image.colorspace_settings.name = 'sRGB'

    # Temporary material
    TMP_MAT = "_UVShellBakeMat_tmp"
    if TMP_MAT in bpy.data.materials:
        bpy.data.materials.remove(bpy.data.materials[TMP_MAT])
    mat = bpy.data.materials.new(name=TMP_MAT)
    if mat.node_tree is None:
        mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()

    bsdf     = nodes.new('ShaderNodeBsdfPrincipled'); bsdf.location     = (   0,    0)
    vc_node  = nodes.new('ShaderNodeVertexColor');    vc_node.location  = (-300,    0)
    img_node = nodes.new('ShaderNodeTexImage');        img_node.location = (-300, -250)
    out_node = nodes.new('ShaderNodeOutputMaterial'); out_node.location  = ( 300,    0)

    vc_node.layer_name = COLOR_LAYER_NAME
    img_node.image = bake_image

    links.new(vc_node.outputs['Color'], bsdf.inputs['Base Color'])
    links.new(bsdf.outputs['BSDF'],     out_node.inputs['Surface'])

    for node in nodes:
        node.select = False
    img_node.select = True
    nodes.active = img_node

    prev_mats = list(mesh.materials)
    mesh.materials.clear()
    mesh.materials.append(mat)

    bk = context.scene.render.bake
    context.scene.cycles.bake_type = 'DIFFUSE'
    bk.use_pass_direct = bk.use_pass_indirect = False
    bk.use_pass_color = True
    bk.margin = 4

    baked_ok = False
    try:
        bpy.ops.object.bake(type='DIFFUSE')
        baked_ok = True
    except Exception as e:
        report({'ERROR'}, f"Bake failed for '{obj.name}': {e}")

    if baked_ok:
        out_path = resolve_output_path(obj.name, prefs)
        bake_image.filepath_raw = out_path
        bake_image.file_format = 'PNG'
        bake_image.save()
        report({'INFO'}, f"Saved → {out_path}")

    # Restore everything
    mesh.materials.clear()
    for m in prev_mats:
        mesh.materials.append(m)
    bpy.data.materials.remove(mat)
    bpy.data.images.remove(bake_image)

    bpy.ops.object.select_all(action='DESELECT')
    for o in prev_selected:
        o.select_set(True)
    context.view_layer.objects.active = prev_active
    context.scene.render.engine = prev_engine

    return baked_ok


# =============================================================================
# MAIN OPERATOR · Colorize (+ optional bake) all listed meshes
# =============================================================================

class UVSHELL_OT_colorize(Operator):
    bl_idname = "uvshell.colorize"
    bl_label = "Colorize UV Islands"
    bl_description = (
        "Colorize UV islands on every mesh in the list. "
        "Optionally bake each one to a diffuse PNG."
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.uv_shell_colorizer
        prefs = context.preferences.addons[__package__].preferences

        targets = [item.obj for item in props.mesh_list if item.obj is not None]

        if not targets:
            self.report({'ERROR'}, "The mesh list is empty. Add at least one mesh.")
            return {'CANCELLED'}

        success_count = 0
        total_shells = 0

        for obj in targets:
            ok, shells = colorize_single_object(obj, self.report)
            if ok:
                success_count += 1
                total_shells += shells

                if prefs.bake_diffuse:
                    bake_diffuse_from_vertex_colors(context, obj, prefs, self.report)

        # Switch all viewports to vertex-color display
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'SOLID'
                        space.shading.color_type = 'VERTEX'

        self.report(
            {'INFO'},
            f"Done: {success_count}/{len(targets)} mesh(es) colorized, "
            f"{total_shells} total UV island(s)."
        )
        return {'FINISHED'}


# =============================================================================
# HELP
# =============================================================================

HELP_SECTIONS = (
    ("1 · Add meshes", (
        "Pick a mesh in the field and press +",
        "Add Selected from Viewport adds every selected mesh",
        "Add from Collection adds its meshes, nested ones too",
    )),
    ("2 · The list", (
        "UVs shows how many UV maps a mesh has",
        "A tick means the mesh is already colorized",
        "The minus button removes one mesh, the bin clears the list",
    )),
    ("3 · Colorize", (
        "Every UV island on the active UV map gets its own color",
        "Islands stacked exactly on top of each other share a color",
        "Colors are stored in an attribute named UV Island Colors",
        "The viewport switches to Solid with attribute colors",
        "Meshes without a UV map are skipped",
        "Running it again picks new colors",
    )),
    ("4 · Bake (optional)", (
        "Tick Bake Diffuse Map to show the bake options",
        "Resolution is 64 to 8192 px, default 1024",
        "Leave Output Folder blank to save next to the .blend",
        "Press Colorize + Bake All",
        "Files are saved as <mesh name>_diffuse_bake.png",
        "Bake settings are kept after restarting Blender",
    )),
)


class UVSHELL_OT_help(Operator):
    bl_idname = "uvshell.help"
    bl_label = "UV Island Colorizer Help"
    bl_description = "Show how to use this tool"

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        for heading, lines in HELP_SECTIONS:
            box = layout.box()
            box.label(text=heading)
            col = box.column(align=True)
            for line in lines:
                col.label(text=line)

    def execute(self, context):
        return {'FINISHED'}


# =============================================================================
# PANEL
# =============================================================================

class UVSHELL_PT_main_panel(Panel):
    bl_label = "UV Island Colorizer"
    bl_idname = "UVSHELL_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Island Colorizer"

    def draw(self, context):
        layout = self.layout
        props = context.scene.uv_shell_colorizer
        prefs = context.preferences.addons[__package__].preferences

        # ------------------------------------------------------------------ #
        # SECTION 1 · Multi-Mesh Picker
        # ------------------------------------------------------------------ #
        box = layout.box()
        box.label(text="1.  Target Meshes", icon='MESH_DATA')

        # ---- Add by name (prop_search · covers scene + all collections) ----
        row = box.row(align=True)
        row.prop(props, "picker_object", text="", icon='OBJECT_DATA')
        add = row.operator("uvshell.add_mesh", text="", icon='ADD')

        # ---- Shortcut: add everything selected in the viewport ----
        box.operator("uvshell.add_selected", text="Add Selected from Viewport", icon='RESTRICT_SELECT_OFF')

        # ---- Shortcut: add all meshes from a collection ----
        box.operator("uvshell.add_collection", text="Add from Collection…", icon='OUTLINER_COLLECTION')

        # ---- The list ----
        row = box.row()
        row.template_list(
            "UVSHELL_UL_mesh_list", "",
            props, "mesh_list",
            props, "mesh_list_index",
            rows=4,
        )

        # List-level buttons (remove / clear)
        col = row.column(align=True)
        col.operator("uvshell.remove_mesh", text="", icon='REMOVE')
        col.separator()
        col.operator("uvshell.clear_list",  text="", icon='TRASH')

        # Summary
        count = len(props.mesh_list)
        if count:
            box.label(text=f"{count} mesh(es) in list", icon='INFO')

        layout.separator()

        # ------------------------------------------------------------------ #
        # SECTION 2 · Options & Action
        # ------------------------------------------------------------------ #
        box2 = layout.box()
        box2.label(text="2.  Colorize & Bake", icon='COLOR')

        # Persistent bake toggle
        box2.prop(prefs, "bake_diffuse", text="Bake Diffuse Map")

        if prefs.bake_diffuse:
            sub = box2.column(align=True)
            sub.prop(prefs, "bake_resolution", text="Resolution (px)")
            sub.prop(prefs, "bake_output_dir", text="Output Folder")
            box2.label(text="Cycles · albedo pass only, no lighting.", icon='INFO')

        layout.separator()

        col = box2.column()
        col.scale_y = 1.6
        op_row = col.row()
        op_row.enabled = len(props.mesh_list) > 0
        label = "Colorize + Bake All" if prefs.bake_diffuse else "Colorize All UV Islands"
        op_row.operator("uvshell.colorize", text=label, icon='BRUSH_DATA')

        layout.separator()
        layout.label(text="Settings saved across restarts.", icon='PREFERENCES')
        layout.operator("uvshell.help", text="Help", icon='HELP')


# =============================================================================
# REGISTRATION
# =============================================================================

classes = (
    UVShellColorizerPreferences,
    MeshListItem,
    UVShellColorizerProps,
    UVSHELL_UL_mesh_list,
    UVSHELL_OT_add_mesh,
    UVSHELL_OT_add_selected,
    UVSHELL_OT_add_collection,
    UVSHELL_OT_remove_mesh,
    UVSHELL_OT_clear_list,
    UVSHELL_OT_colorize,
    UVSHELL_OT_help,
    UVSHELL_PT_main_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.uv_shell_colorizer = PointerProperty(type=UVShellColorizerProps)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.uv_shell_colorizer


if __name__ == "__main__":
    register()