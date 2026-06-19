bl_info = {
    "name": "UV Scroll (Texture Panner)",
    "author": "LandmineGirl",
    "version": (1, 2, 2),
    "blender": (4, 0, 0),
    "location": "Material Properties > UV Scroll; 3D View > N-panel > UV Scroll",
    "description": "Scroll/pan UVs on textures with controllable speed/direction per material, unique-dup setup, material list, and seamless looping.",
    "category": "Material",
}

import bpy
import math
from bpy.app.handlers import persistent

GROUP_NAME = "LMG_UV_Scroller"

# -----------------------------
# Helpers
# -----------------------------
def _get_socket(sockets, name, fallback_index=None):
    try:
        sock = sockets.get(name)
        if sock is not None:
            return sock
    except Exception:
        pass
    if fallback_index is not None and fallback_index < len(sockets):
        return sockets[fallback_index]
    return None

def _group_ok(ng):
    try:
        items = list(ng.interface.items_tree)
        ins  = [i.name for i in items if getattr(i, "item_type", "") == "SOCKET" and getattr(i, "in_out", "") == 'INPUT']
        outs = [i.name for i in items if getattr(i, "item_type", "") == "SOCKET" and getattr(i, "in_out", "") == 'OUTPUT']
        return ('UV' in ins) and ('Offset' in ins) and ('UV Out' in outs)
    except Exception:
        return False

def _unique_name(base, existing_names):
    i = 1
    name = f"{base}_old"
    while name in existing_names:
        i += 1
        name = f"{base}_old_{i}"
    return name

def _unique_mat_name(base):
    names = {m.name for m in bpy.data.materials}
    name = f"{base}_UVS"
    i = 1
    while name in names:
        i += 1
        name = f"{base}_UVS.{i:03d}"
    return name

def _scene_fps(scene):
    fps = scene.render.fps
    base = scene.render.fps_base if scene.render.fps_base else 1.0
    return fps / base

def _loop_frames(scene):
    if getattr(scene, "use_preview_range", False):
        start = scene.frame_preview_start
        end   = scene.frame_preview_end
    else:
        start = scene.frame_start
        end   = scene.frame_end
    return max(1, int(end - start + 1)), int(start)

def _material_scroller_nodes(mat):
    if not mat or not mat.node_tree:
        return []
    return [
        n for n in mat.node_tree.nodes
        if n.type == 'GROUP' and getattr(n, "node_tree", None) and n.node_tree.name == GROUP_NAME
    ]

def _compute_vx_vy_raw(mat):
    mode = getattr(mat, "uvscroll_mode", 'XY')
    if mode == 'ANGLE':
        spd = getattr(mat, "uvscroll_speed", 0.0)
        ang = getattr(mat, "uvscroll_angle", 0.0)  # radians
        return spd * math.cos(ang), spd * math.sin(ang)
    return getattr(mat, "uvscroll_speed_x", 0.0), getattr(mat, "uvscroll_speed_y", 0.0)

def _quantize_velocity_for_loop(vx, vy, scene, enabled=True):
    if not enabled:
        return vx, vy
    fps = _scene_fps(scene)
    if fps <= 0.0:
        return vx, vy
    frames, _ = _loop_frames(scene)
    if frames <= 0:
        return vx, vy
    q = fps / frames  # tiles/sec so q*(frames/fps) == 1 tile
    vx_q = round(vx / q) * q
    vy_q = round(vy / q) * q
    return vx_q, vy_q

def _update_offsets_for_material(mat, scene=None):
    if not mat or not getattr(mat, "uvscroll_enabled", False):
        return
    if scene is None:
        scene = bpy.context.scene
        if scene is None:
            return

    fps = _scene_fps(scene)
    t = (scene.frame_current) / fps if fps > 0 else 0.0

    vx, vy = _compute_vx_vy_raw(mat)
    vx, vy = _quantize_velocity_for_loop(vx, vy, scene, getattr(mat, "uvscroll_seamless", True))

    base_x = getattr(mat, "uvscroll_offset_x", 0.0)
    base_y = getattr(mat, "uvscroll_offset_y", 0.0)
    off_x = base_x + vx * t
    off_y = base_y + vy * t
    if getattr(mat, "uvscroll_wrap", True):
        off_x %= 1.0
        off_y %= 1.0

    for gnode in _material_scroller_nodes(mat):
        in_off = _get_socket(gnode.inputs, 'Offset', 1)
        if in_off and hasattr(in_off, "default_value"):
            in_off.default_value = (off_x, off_y, 0.0)

# -----------------------------
# Node Group (Blender 4.x interface API)
# -----------------------------
def ensure_scroller_group():
    ngs = getattr(bpy.data, "node_groups", None)
    if ngs is None:
        return None

    ng = ngs.get(GROUP_NAME)
    if ng and _group_ok(ng):
        return ng

    if ng and not _group_ok(ng):
        try:
            ng.name = _unique_name(GROUP_NAME, {g.name for g in ngs})
        except Exception:
            pass
        ng = None

    if ng is None:
        ng = ngs.new(GROUP_NAME, 'ShaderNodeTree')
        iface = ng.interface
        iface.new_socket(name="UV",      in_out='INPUT',  socket_type='NodeSocketVector')
        iface.new_socket(name="Offset",  in_out='INPUT',  socket_type='NodeSocketVector')
        iface.new_socket(name="UV Out",  in_out='OUTPUT', socket_type='NodeSocketVector')

        nodes = ng.nodes; links = ng.links
        nodes.clear()

        n_in  = nodes.new("NodeGroupInput");       n_in.location  = (-300, 0)
        n_add = nodes.new("ShaderNodeVectorMath"); n_add.location = (0, 0); n_add.operation = 'ADD'
        n_out = nodes.new("NodeGroupOutput");      n_out.location = (220, 0)

        gi_uv     = _get_socket(n_in.outputs,  "UV",     0)
        gi_offset = _get_socket(n_in.outputs,  "Offset", 1)
        go_uv     = _get_socket(n_out.inputs,  "UV Out", 0)

        links.new(gi_uv, n_add.inputs[0])
        links.new(gi_offset, n_add.inputs[1])
        links.new(n_add.outputs['Vector'], go_uv)

    return ng

# -----------------------------
# SAFE REWIRING (fixes your crash)
# -----------------------------
def _safe_insert_group_before_input(links, input_socket, group_in_uv, group_out_uv, default_from_socket):
    """
    Insert a group between whatever is driving `input_socket` and the input itself:
      previous_source --(new)--> group_in_uv
      group_out_uv    --(new)--> input_socket
    Old links on `input_socket` are removed only AFTER creating the new link from the captured source.
    """
    if input_socket is None:
        return

    prev_links = list(input_socket.links)  # capture before we modify anything
    if prev_links:
        prev_from_socket = prev_links[0].from_socket  # this pointer stays valid after we remove the link
        # connect previous source to group
        links.new(prev_from_socket, group_in_uv)
        # now remove ALL old links on the Image Texture's Vector input
        for l in prev_links:
            try:
                links.remove(l)
            except ReferenceError:
                pass
    else:
        # no previous source -> use provided default (TexCoord UV)
        links.new(default_from_socket, group_in_uv)

    # finally wire group output to the image texture input
    links.new(group_out_uv, input_socket)

# -----------------------------
# Wiring / Duplication
# -----------------------------
def connect_scroller_in_material(mat: bpy.types.Material):
    if not mat:
        return
    if not mat.use_nodes:
        mat.use_nodes = True
    nt = mat.node_tree
    if nt is None:
        return

    ng = ensure_scroller_group()
    if ng is None:
        return

    nodes = nt.nodes
    links = nt.links

    texcoord = next((n for n in nodes if n.type == 'TEX_COORD'), None)
    if not texcoord:
        texcoord = nodes.new('ShaderNodeTexCoord')
        texcoord.location = (-1200, 0)

    image_nodes = [n for n in nodes if n.type == 'TEX_IMAGE']

    for img in image_nodes:
        vec_input = img.inputs.get('Vector')
        if vec_input is None:
            continue

        # Skip if already has our group right before it
        if vec_input.is_linked:
            from_node = vec_input.links[0].from_node
            if (from_node.type == 'GROUP'
                and getattr(from_node, "node_tree", None)
                and from_node.node_tree.name == GROUP_NAME):
                continue

        gnode = nodes.new('ShaderNodeGroup')
        gnode.node_tree = ng
        gnode.label = "UV SCROLL"
        gnode.width = 160
        gnode.location = img.location.x - 300, img.location.y

        in_uv   = _get_socket(gnode.inputs,  'UV',      0)
        in_off  = _get_socket(gnode.inputs,  'Offset',  1)
        out_uv  = _get_socket(gnode.outputs, 'UV Out',  0)

        if in_uv is None or out_uv is None:
            nodes.remove(gnode)
            continue

        try:
            _safe_insert_group_before_input(
                links=links,
                input_socket=vec_input,
                group_in_uv=in_uv,
                group_out_uv=out_uv,
                default_from_socket=texcoord.outputs['UV']
            )

            offx = getattr(mat, "uvscroll_offset_x", 0.0)
            offy = getattr(mat, "uvscroll_offset_y", 0.0)
            if in_off and hasattr(in_off, "default_value"):
                in_off.default_value = (offx, offy, 0.0)

        except Exception:
            try:
                nodes.remove(gnode)
            except Exception:
                pass
            continue

def _uniquify_materials_on_object(obj):
    if not obj or not getattr(obj, "material_slots", None):
        return 0
    made = 0
    cache = {}
    for i, slot in enumerate(obj.material_slots):
        mat = slot.material
        if not mat:
            continue
        if mat.name in cache:
            slot.material = cache[mat.name]
            continue
        new = mat.copy()
        new.name = _unique_mat_name(mat.name)
        slot.material = new
        cache[mat.name] = new
        made += 1
    return made

def connect_scroller_for_selected_objects(make_unique=True):
    sel = list(bpy.context.selected_objects)
    for obj in sel:
        if make_unique:
            _uniquify_materials_on_object(obj)
        for slot in getattr(obj, "material_slots", []):
            mat = slot.material
            if mat:
                connect_scroller_in_material(mat)

# -----------------------------
# Material Properties (live updates)
# -----------------------------
def _mat_update_connect(self, context):
    if getattr(self, "uvscroll_enabled", False):
        connect_scroller_in_material(self)
    _update_offsets_for_material(self, context.scene if context else None)

def _mat_update_refresh(self, context):
    _update_offsets_for_material(self, context.scene if context else None)

def register_material_props():
    bpy.types.Material.uvscroll_enabled = bpy.props.BoolProperty(
        name="Enable UV Scroll",
        description="Enable UV scrolling for this material",
        default=False,
        update=_mat_update_connect,
    )
    bpy.types.Material.uvscroll_mode = bpy.props.EnumProperty(
        name="Mode",
        description="Control UV scroll by X/Y speeds or Speed+Angle",
        items=(
            ('XY', "X/Y", "Set independent speeds for U (X) and V (Y)"),
            ('ANGLE', "Angle", "Set a speed and direction (° shown; stored in radians)"),
        ),
        default='XY',
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_speed_x = bpy.props.FloatProperty(
        name="Speed U (X)",
        description="UV tiles per second along U axis (positive is right)",
        default=0.1, min=-100.0, max=100.0,
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_speed_y = bpy.props.FloatProperty(
        name="Speed V (Y)",
        description="UV tiles per second along V axis (positive is up)",
        default=0.0, min=-100.0, max=100.0,
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_speed = bpy.props.FloatProperty(
        name="Speed",
        description="Overall speed in UV tiles per second",
        default=0.1, min=0.0, max=100.0,
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_angle = bpy.props.FloatProperty(
        name="Direction (°)",
        description="Direction; UI is degrees, value is stored in radians (0° = +U/right, 90° = +V/up)",
        default=0.0, subtype='ANGLE',
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_offset_x = bpy.props.FloatProperty(
        name="Base Offset U",
        description="Starting U offset (0..1)",
        default=0.0,
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_offset_y = bpy.props.FloatProperty(
        name="Base Offset V",
        description="Starting V offset (0..1)",
        default=0.0,
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_wrap = bpy.props.BoolProperty(
        name="Wrap 0..1",
        description="Keep offsets within 0..1 so tiles loop cleanly",
        default=True,
        update=_mat_update_refresh,
    )
    bpy.types.Material.uvscroll_seamless = bpy.props.BoolProperty(
        name="Seamless Loop",
        description="Quantize speed so the scroll loops perfectly over the current playback range",
        default=True,
        update=_mat_update_refresh,
    )

def unregister_material_props():
    for attr in [
        "uvscroll_enabled", "uvscroll_mode",
        "uvscroll_speed_x", "uvscroll_speed_y",
        "uvscroll_speed", "uvscroll_angle",
        "uvscroll_offset_x", "uvscroll_offset_y",
        "uvscroll_wrap", "uvscroll_seamless"
    ]:
        if hasattr(bpy.types.Material, attr):
            delattr(bpy.types.Material, attr)

# -----------------------------
# Frame Handler
# -----------------------------
@persistent
def uvscroll_frame_update(scene):
    fps = _scene_fps(scene)
    t = scene.frame_current / fps if fps > 0 else 0.0
    for mat in bpy.data.materials:
        if not getattr(mat, "uvscroll_enabled", False) or not mat.use_nodes:
            continue
        vx, vy = _compute_vx_vy_raw(mat)
        vx, vy = _quantize_velocity_for_loop(vx, vy, scene, getattr(mat, "uvscroll_seamless", True))

        base_x = getattr(mat, "uvscroll_offset_x", 0.0)
        base_y = getattr(mat, "uvscroll_offset_y", 0.0)

        off_x = base_x + vx * t
        off_y = base_y + vy * t
        if getattr(mat, "uvscroll_wrap", True):
            off_x %= 1.0
            off_y %= 1.0

        for gnode in _material_scroller_nodes(mat):
            in_off = _get_socket(gnode.inputs, 'Offset', 1)
            if in_off and hasattr(in_off, "default_value"):
                in_off.default_value = (off_x, off_y, 0.0)

# -----------------------------
# UI
# -----------------------------
def _active_material(context):
    obj = getattr(context, "object", None)
    if obj and obj.active_material:
        return obj.active_material
    return getattr(context, "material", None)

class LMG_PT_UVScrollMaterial(bpy.types.Panel):
    bl_label = "UV Scroll"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "material"

    @classmethod
    def poll(cls, context):
        return getattr(context, "material", None) is not None

    def draw(self, context):
        layout = self.layout
        mat = context.material

        row = layout.row(align=True)
        row.prop(mat, "uvscroll_enabled", toggle=True)
        row.operator("lmg_uvscroll.setup_this_material", text="Setup Nodes", icon='NODETREE')

        col = layout.column(align=True)
        col.prop(mat, "uvscroll_mode")

        if mat.uvscroll_mode == 'ANGLE':
            col.prop(mat, "uvscroll_speed")
            col.prop(mat, "uvscroll_angle")
        else:
            col.prop(mat, "uvscroll_speed_x")
            col.prop(mat, "uvscroll_speed_y")

        box = layout.box()
        box.label(text="Base Offset", icon='UV')
        r = box.row(align=True)
        r.prop(mat, "uvscroll_offset_x", slider=True)
        r.prop(mat, "uvscroll_offset_y", slider=True)
        box.prop(mat, "uvscroll_wrap")
        box.prop(mat, "uvscroll_seamless")

class LMG_PT_UVScrollView3D(bpy.types.Panel):
    bl_label = "UV Scroll"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Scroll"

    def draw(self, context):
        layout = self.layout
        obj = getattr(context, "object", None)

        box = layout.box()
        box.label(text="Object Materials", icon='MATERIAL')
        if obj and getattr(obj, "material_slots", None):
            for i, slot in enumerate(obj.material_slots):
                mat = slot.material
                row = box.row(align=True)
                if mat:
                    row.label(text=f"{i}: {mat.name}", icon='MATERIAL')
                    row.prop(mat, "uvscroll_enabled", text="Enabled", toggle=True)
                    op = row.operator("lmg_uvscroll.setup_one_slot", text="", icon='NODETREE')
                    op.object_name = obj.name
                    op.slot_index = i
                else:
                    row.label(text=f"{i}: (empty)", icon='ERROR')
        else:
            box.label(text="No materials on active object.", icon='INFO')

        layout.separator()
        layout.operator("lmg_uvscroll.setup_selected", icon='MATERIAL')
        layout.operator("lmg_uvscroll.enable_for_selected", text="Enable on All Materials (Selected)", icon='CHECKMARK')

        mat = _active_material(context)
        if mat:
            layout.separator()
            col = layout.column(align=True)
            col.label(text=f"Active Material: {mat.name}", icon='DOT')
            row = col.row(align=True)
            row.prop(mat, "uvscroll_enabled", toggle=True, text="Enabled")
            row.operator("lmg_uvscroll.setup_this_material", text="", icon='NODETREE')

            col.prop(mat, "uvscroll_mode", text="Mode")
            if mat.uvscroll_mode == 'ANGLE':
                col.prop(mat, "uvscroll_speed", text="Speed (tiles/s)")
                col.prop(mat, "uvscroll_angle", text="Direction")
            else:
                col.prop(mat, "uvscroll_speed_x", text="Speed U (tiles/s)")
                col.prop(mat, "uvscroll_speed_y", text="Speed V (tiles/s)")
            b = col.box()
            b.label(text="Base Offset", icon='UV')
            rr = b.row(align=True)
            rr.prop(mat, "uvscroll_offset_x", text="U")
            rr.prop(mat, "uvscroll_offset_y", text="V")
            b.prop(mat, "uvscroll_wrap", text="Wrap 0..1")
            b.prop(mat, "uvscroll_seamless", text="Seamless Loop")

# -----------------------------
# Operators
# -----------------------------
class LMG_OT_SetupOneSlot(bpy.types.Operator):
    bl_idname = "lmg_uvscroll.setup_one_slot"
    bl_label = "Setup (This Slot)"
    bl_description = "Duplicate this slot's material and wire scrolling nodes"
    bl_options = {'REGISTER', 'UNDO'}

    object_name: bpy.props.StringProperty()
    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if not obj or self.slot_index < 0 or self.slot_index >= len(obj.material_slots):
            self.report({'WARNING'}, "Invalid object/slot.")
            return {'CANCELLED'}
        slot = obj.material_slots[self.slot_index]
        if not slot.material:
            self.report({'WARNING'}, "Slot has no material.")
            return {'CANCELLED'}
        new = slot.material.copy()
        new.name = _unique_mat_name(slot.material.name)
        slot.material = new
        connect_scroller_in_material(new)
        _update_offsets_for_material(new, context.scene)
        self.report({'INFO'}, f"Uniquified & setup '{new.name}'")
        return {'FINISHED'}

class LMG_OT_SetupThisMaterial(bpy.types.Operator):
    bl_idname = "lmg_uvscroll.setup_this_material"
    bl_label = "Setup UV Scroll in Material"
    bl_description = "Insert UV scroller nodes for this material's image textures"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        mat_ctx = getattr(context, "material", None)
        if mat_ctx is not None:
            return True
        obj = getattr(context, "object", None)
        return bool(obj and obj.active_material)

    def execute(self, context):
        mat = getattr(context, "material", None) or _active_material(context)
        if not mat:
            self.report({'WARNING'}, "No active material.")
            return {'CANCELLED'}
        connect_scroller_in_material(mat)
        _update_offsets_for_material(mat, context.scene)
        self.report({'INFO'}, f"UV scroller set in '{mat.name}'")
        return {'FINISHED'}

class LMG_OT_SetupSelected(bpy.types.Operator):
    bl_idname = "lmg_uvscroll.setup_selected"
    bl_label = "Setup for Selected Objects"
    bl_description = "Duplicate materials per slot on selected objects, then wire UV scroller to their Image Textures"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        sel = getattr(context, "selected_objects", [])
        return bool(sel)

    def execute(self, context):
        count_slots = 0
        for obj in context.selected_objects:
            count_slots += _uniquify_materials_on_object(obj)
        connect_scroller_for_selected_objects(make_unique=False)
        for obj in context.selected_objects:
            for slot in getattr(obj, "material_slots", []):
                if slot.material:
                    _update_offsets_for_material(slot.material, context.scene)
        self.report({'INFO'}, f"Duplicated & setup materials on {count_slots} slot(s)")
        return {'FINISHED'}

class LMG_OT_EnableForSelected(bpy.types.Operator):
    bl_idname = "lmg_uvscroll.enable_for_selected"
    bl_label = "Enable UV Scroll for Selected Objects"
    bl_description = "Turn on UV scroll for all materials on selected objects and wire nodes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        sel = getattr(context, "selected_objects", [])
        return bool(sel)

    def execute(self, context):
        count = 0
        for obj in context.selected_objects:
            for slot in getattr(obj, "material_slots", []):
                mat = slot.material
                if not mat:
                    continue
                if not getattr(mat, "uvscroll_enabled", False):
                    mat.uvscroll_enabled = True
                else:
                    connect_scroller_in_material(mat)
                    _update_offsets_for_material(mat, context.scene)
                count += 1
        self.report({'INFO'}, f"Enabled UV scroll on {count} material slot(s)")
        return {'FINISHED'}

# -----------------------------
# Registration
# -----------------------------
_classes = (
    LMG_PT_UVScrollMaterial,
    LMG_PT_UVScrollView3D,
    LMG_OT_SetupOneSlot,
    LMG_OT_SetupThisMaterial,
    LMG_OT_SetupSelected,
    LMG_OT_EnableForSelected,
)

def register():
    register_material_props()
    for cls in _classes:
        bpy.utils.register_class(cls)
    if uvscroll_frame_update not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(uvscroll_frame_update)

def unregister():
    if uvscroll_frame_update in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(uvscroll_frame_update)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    unregister_material_props()

if __name__ == "__main__":
    register()
