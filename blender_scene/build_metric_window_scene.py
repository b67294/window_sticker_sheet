import bpy
import json
import math
import os
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

ROOM_WIDTH_M = 4.0
ROOM_DEPTH_M = 5.0
ROOM_HEIGHT_M = 2.8
WALL_THICKNESS_M = 0.12

# The glass is the exact, usable sticker target surface.
WINDOW_WIDTH_M = 0.450
WINDOW_HEIGHT_M = 0.600
WINDOW_BOTTOM_M = 0.850
WINDOW_CENTER_Z_M = WINDOW_BOTTOM_M + WINDOW_HEIGHT_M / 2.0
FRAME_WIDTH_M = 0.050
FRAME_DEPTH_M = 0.045

IMAGE_WIDTH_PX = 2048
IMAGE_HEIGHT_PX = 1536
CAMERA_FOCAL_LENGTH_MM = 50.0
CAMERA_SENSOR_WIDTH_MM = 36.0
CAMERA_DISTANCE_M = 4.70
CAMERA_Z_M = 1.55
CAMERA_TARGET_Z_M = 1.25

BACK_WALL_Y = ROOM_DEPTH_M / 2.0
INTERIOR_WALL_Y = BACK_WALL_Y - WALL_THICKNESS_M / 2.0
GLASS_Y = INTERIOR_WALL_Y - 0.006


def reset_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.cameras,
        bpy.data.lights,
    ):
        for datablock in list(datablocks):
            if datablock.users == 0:
                datablocks.remove(datablock)


def material(name, base_color, roughness=0.72, metallic=0.0):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*base_color, 1.0)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*base_color, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    return mat


def emission_material(name, color=(1.0, 1.0, 1.0, 1.0), strength=1.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = strength
    mat.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return mat


def add_box(name, location, dimensions, mat, role):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    obj["role"] = role
    return obj


def add_area_light(name, location, energy, size, color):
    data = bpy.data.lights.new(name=name, type="AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    data.color = color
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    point_at(obj, Vector((0.0, 1.6, 1.2)))
    return obj


def point_at(obj, target):
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def create_camera(name, horizontal_angle_deg):
    angle = math.radians(horizontal_angle_deg)
    target = Vector((0.0, GLASS_Y, CAMERA_TARGET_Z_M))
    camera_location = Vector(
        (
            -CAMERA_DISTANCE_M * math.sin(angle),
            GLASS_Y - CAMERA_DISTANCE_M * math.cos(angle),
            CAMERA_Z_M,
        )
    )
    camera_data = bpy.data.cameras.new(name=name)
    camera_data.lens = CAMERA_FOCAL_LENGTH_MM
    camera_data.sensor_width = CAMERA_SENSOR_WIDTH_MM
    camera_data.sensor_fit = "HORIZONTAL"
    camera_obj = bpy.data.objects.new(name, camera_data)
    bpy.context.collection.objects.link(camera_obj)
    camera_obj.location = camera_location
    point_at(camera_obj, target)
    camera_obj["horizontal_angle_deg"] = horizontal_angle_deg
    return camera_obj


def world_point_to_pixel(scene, camera, point):
    ndc = world_to_camera_view(scene, camera, Vector(point))
    return {
        "x": round(float(ndc.x) * IMAGE_WIDTH_PX, 3),
        "y": round((1.0 - float(ndc.y)) * IMAGE_HEIGHT_PX, 3),
        "normalized_x": round(float(ndc.x), 8),
        "normalized_y": round(1.0 - float(ndc.y), 8),
        "depth_camera_space_m": round(float(ndc.z), 8),
    }


def vector_list(value):
    return [round(float(component), 8) for component in value]


def configure_scene(scene):
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = IMAGE_WIDTH_PX
    scene.render.resolution_y = IMAGE_HEIGHT_PX
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.quality = 95
    scene.render.film_transparent = False
    scene.render.use_file_extension = True
    scene.render.resolution_percentage = 100
    scene.render.image_settings.color_depth = "8"
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.world.color = (0.055, 0.055, 0.055)


def build_room():
    wall_mat = material("Clay_Walls", (0.83, 0.83, 0.81), roughness=0.82)
    floor_mat = material("Clay_Floor", (0.63, 0.64, 0.64), roughness=0.88)
    frame_mat = material("Clay_Frame", (0.94, 0.94, 0.92), roughness=0.55)
    glass_mat = material("Clay_Glass", (0.42, 0.53, 0.58), roughness=0.30)

    add_box(
        "Floor",
        (0.0, 0.0, -0.04),
        (ROOM_WIDTH_M, ROOM_DEPTH_M, 0.08),
        floor_mat,
        "room",
    )
    add_box(
        "LeftWall",
        (-ROOM_WIDTH_M / 2.0 - WALL_THICKNESS_M / 2.0, 0.0, ROOM_HEIGHT_M / 2.0),
        (WALL_THICKNESS_M, ROOM_DEPTH_M, ROOM_HEIGHT_M),
        wall_mat,
        "room",
    )
    add_box(
        "RightWall",
        (ROOM_WIDTH_M / 2.0 + WALL_THICKNESS_M / 2.0, 0.0, ROOM_HEIGHT_M / 2.0),
        (WALL_THICKNESS_M, ROOM_DEPTH_M, ROOM_HEIGHT_M),
        wall_mat,
        "room",
    )
    add_box(
        "Ceiling",
        (0.0, 0.0, ROOM_HEIGHT_M + 0.04),
        (ROOM_WIDTH_M, ROOM_DEPTH_M, 0.08),
        wall_mat,
        "room",
    )

    opening_width = WINDOW_WIDTH_M + 2.0 * FRAME_WIDTH_M
    opening_bottom = WINDOW_BOTTOM_M - FRAME_WIDTH_M
    opening_top = WINDOW_BOTTOM_M + WINDOW_HEIGHT_M + FRAME_WIDTH_M

    add_box(
        "BackWall_Below",
        (0.0, BACK_WALL_Y, opening_bottom / 2.0),
        (ROOM_WIDTH_M, WALL_THICKNESS_M, opening_bottom),
        wall_mat,
        "room",
    )
    upper_height = ROOM_HEIGHT_M - opening_top
    add_box(
        "BackWall_Above",
        (0.0, BACK_WALL_Y, opening_top + upper_height / 2.0),
        (ROOM_WIDTH_M, WALL_THICKNESS_M, upper_height),
        wall_mat,
        "room",
    )
    side_width = (ROOM_WIDTH_M - opening_width) / 2.0
    middle_height = opening_top - opening_bottom
    for side, x in (
        ("Left", -(opening_width / 2.0 + side_width / 2.0)),
        ("Right", opening_width / 2.0 + side_width / 2.0),
    ):
        add_box(
            f"BackWall_{side}",
            (x, BACK_WALL_Y, opening_bottom + middle_height / 2.0),
            (side_width, WALL_THICKNESS_M, middle_height),
            wall_mat,
            "room",
        )

    vertical_frame_height = WINDOW_HEIGHT_M + 2.0 * FRAME_WIDTH_M
    for side, x in (
        ("Left", -(WINDOW_WIDTH_M / 2.0 + FRAME_WIDTH_M / 2.0)),
        ("Right", WINDOW_WIDTH_M / 2.0 + FRAME_WIDTH_M / 2.0),
    ):
        add_box(
            f"WindowFrame_{side}",
            (x, GLASS_Y - FRAME_DEPTH_M / 2.0, WINDOW_CENTER_Z_M),
            (FRAME_WIDTH_M, FRAME_DEPTH_M, vertical_frame_height),
            frame_mat,
            "window_frame",
        )
    horizontal_frame_width = WINDOW_WIDTH_M
    for side, z in (
        ("Bottom", WINDOW_BOTTOM_M - FRAME_WIDTH_M / 2.0),
        ("Top", WINDOW_BOTTOM_M + WINDOW_HEIGHT_M + FRAME_WIDTH_M / 2.0),
    ):
        add_box(
            f"WindowFrame_{side}",
            (0.0, GLASS_Y - FRAME_DEPTH_M / 2.0, z),
            (horizontal_frame_width, FRAME_DEPTH_M, FRAME_WIDTH_M),
            frame_mat,
            "window_frame",
        )

    glass = add_box(
        "StickerTargetGlass_450x600mm",
        (0.0, GLASS_Y, WINDOW_CENTER_Z_M),
        (WINDOW_WIDTH_M, 0.008, WINDOW_HEIGHT_M),
        glass_mat,
        "glass_target",
    )
    return glass, glass_mat


def render_clay(scene, camera, stem):
    scene.camera = camera
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.quality = 95
    scene.render.filepath = os.path.join(OUTPUT_DIR, f"{stem}_clay.jpg")
    bpy.ops.render.render(write_still=True)


def render_mask(scene, camera, glass, original_glass_material, stem):
    renderable = [obj for obj in scene.objects if obj.type in {"MESH", "LIGHT"}]
    visibility = {obj.name: obj.hide_render for obj in renderable}
    world_color = tuple(scene.world.color)
    original_world_use_nodes = scene.world.use_nodes
    scene.world.use_nodes = True
    world_background = scene.world.node_tree.nodes.get("Background")
    original_background_color = tuple(world_background.inputs["Color"].default_value)
    original_background_strength = world_background.inputs["Strength"].default_value
    original_view_transform = scene.view_settings.view_transform
    original_look = scene.view_settings.look
    original_exposure = scene.view_settings.exposure
    original_gamma = scene.view_settings.gamma
    for obj in renderable:
        obj.hide_render = obj != glass

    mask_material = emission_material("Mask_White_Emission")
    glass.data.materials.clear()
    glass.data.materials.append(mask_material)
    scene.world.color = (0.0, 0.0, 0.0)
    world_background.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    world_background.inputs["Strength"].default_value = 0.0
    scene.camera = camera
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "BW"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False
    # Data masks must bypass the AgX display transform so black/white remain 0/255.
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.render.filepath = os.path.join(OUTPUT_DIR, f"{stem}_glass_mask.png")
    bpy.ops.render.render(write_still=True)

    glass.data.materials.clear()
    glass.data.materials.append(original_glass_material)
    for obj in renderable:
        obj.hide_render = visibility[obj.name]
    scene.world.color = world_color
    world_background.inputs["Color"].default_value = original_background_color
    world_background.inputs["Strength"].default_value = original_background_strength
    scene.world.use_nodes = original_world_use_nodes
    scene.view_settings.view_transform = original_view_transform
    scene.view_settings.look = original_look
    scene.view_settings.exposure = original_exposure
    scene.view_settings.gamma = original_gamma


def main():
    reset_scene()
    scene = bpy.context.scene
    configure_scene(scene)
    glass, glass_material = build_room()

    add_area_light(
        "KeyLight",
        (-1.2, -1.5, 2.45),
        energy=850.0,
        size=3.0,
        color=(1.0, 0.93, 0.84),
    )
    add_area_light(
        "FillLight",
        (1.7, 0.1, 2.15),
        energy=520.0,
        size=2.2,
        color=(0.78, 0.88, 1.0),
    )

    camera_specs = [
        ("front", 0.0),
        ("left_10deg", 10.0),
        ("right_10deg", -10.0),
    ]
    cameras = [(stem, create_camera(f"Camera_{stem}", angle)) for stem, angle in camera_specs]

    scene_json = {
        "schema_version": "1.0",
        "units": {"world": "meter", "physical_dimensions": "millimeter", "image": "pixel"},
        "room": {
            "width_mm": int(ROOM_WIDTH_M * 1000),
            "depth_mm": int(ROOM_DEPTH_M * 1000),
            "height_mm": int(ROOM_HEIGHT_M * 1000),
        },
        "window_target_surface": {
            "interpretation": "exact usable glass/sticker plane; frame is outside this size",
            "width_mm": int(WINDOW_WIDTH_M * 1000),
            "height_mm": int(WINDOW_HEIGHT_M * 1000),
            "bottom_from_floor_mm": int(WINDOW_BOTTOM_M * 1000),
            "center_world_m": [0.0, round(GLASS_Y, 8), round(WINDOW_CENTER_Z_M, 8)],
        },
        "render": {
            "width_px": IMAGE_WIDTH_PX,
            "height_px": IMAGE_HEIGHT_PX,
            "pixel_origin": "top-left",
        },
        "window_corner_order": ["top_left", "top_right", "bottom_right", "bottom_left"],
        "cameras": {},
    }

    window_corners_world = {
        "top_left": (-WINDOW_WIDTH_M / 2.0, GLASS_Y - 0.0041, WINDOW_BOTTOM_M + WINDOW_HEIGHT_M),
        "top_right": (WINDOW_WIDTH_M / 2.0, GLASS_Y - 0.0041, WINDOW_BOTTOM_M + WINDOW_HEIGHT_M),
        "bottom_right": (WINDOW_WIDTH_M / 2.0, GLASS_Y - 0.0041, WINDOW_BOTTOM_M),
        "bottom_left": (-WINDOW_WIDTH_M / 2.0, GLASS_Y - 0.0041, WINDOW_BOTTOM_M),
    }

    for stem, camera in cameras:
        render_clay(scene, camera, stem)
        render_mask(scene, camera, glass, glass_material, stem)
        scene_json["cameras"][stem] = {
            "horizontal_angle_deg": float(camera["horizontal_angle_deg"]),
            "focal_length_mm": camera.data.lens,
            "sensor_width_mm": camera.data.sensor_width,
            "location_world_m": vector_list(camera.location),
            "rotation_euler_radians": vector_list(camera.rotation_euler),
            "clay_image": f"{stem}_clay.jpg",
            "glass_mask": f"{stem}_glass_mask.png",
            "window_corners": {
                corner_name: {
                    "world_m": vector_list(world_position),
                    "pixel": world_point_to_pixel(scene, camera, world_position),
                }
                for corner_name, world_position in window_corners_world.items()
            },
        }

    json_path = os.path.join(OUTPUT_DIR, "window_corners.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(scene_json, handle, ensure_ascii=False, indent=2)

    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.color_mode = "RGB"
    scene.camera = cameras[0][1]
    blend_path = os.path.join(OUTPUT_DIR, "metric_window_room.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    print(f"Generated scene and renders in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
