import importlib.util
import json
import math
import os
from pathlib import Path

import bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view


SCRIPT_DIR = Path(__file__).resolve().parent
BASE_SCRIPT = SCRIPT_DIR.parent / "blender_scene" / "build_metric_window_scene.py"
OUTPUT_DIR = SCRIPT_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("single_window_base", BASE_SCRIPT)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

ROOM_WIDTH_M = 4.0
ROOM_DEPTH_M = 5.0
ROOM_HEIGHT_M = 2.8
WALL_THICKNESS_M = 0.12

# The complete twin-window sticker envelope is exactly 600 x 600 mm.
TARGET_WIDTH_M = 0.600
TARGET_HEIGHT_M = 0.600
WINDOW_BOTTOM_M = 0.850
WINDOW_CENTER_Z_M = WINDOW_BOTTOM_M + TARGET_HEIGHT_M / 2.0
CENTER_MULLION_M = 0.028
PANE_WIDTH_M = (TARGET_WIDTH_M - CENTER_MULLION_M) / 2.0
FRAME_WIDTH_M = 0.035
FRAME_DEPTH_M = 0.035

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


def create_camera(name, horizontal_angle_deg):
    angle = math.radians(horizontal_angle_deg)
    target = Vector((0.0, GLASS_Y, CAMERA_TARGET_Z_M))
    location = Vector(
        (
            -CAMERA_DISTANCE_M * math.sin(angle),
            GLASS_Y - CAMERA_DISTANCE_M * math.cos(angle),
            CAMERA_Z_M,
        )
    )
    data = bpy.data.cameras.new(name=name)
    data.lens = CAMERA_FOCAL_LENGTH_MM
    data.sensor_width = CAMERA_SENSOR_WIDTH_MM
    data.sensor_fit = "HORIZONTAL"
    camera = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(camera)
    camera.location = location
    base.point_at(camera, target)
    camera["horizontal_angle_deg"] = horizontal_angle_deg
    return camera


def build_room_and_double_window():
    wall_mat = base.material("Clay_Walls", (0.83, 0.83, 0.81), roughness=0.82)
    floor_mat = base.material("Clay_Floor", (0.63, 0.64, 0.64), roughness=0.88)
    frame_mat = base.material("Clay_Frame", (0.94, 0.94, 0.92), roughness=0.55)
    glass_mat = base.material("Clay_Glass", (0.42, 0.53, 0.58), roughness=0.30)

    base.add_box(
        "Floor", (0.0, 0.0, -0.04),
        (ROOM_WIDTH_M, ROOM_DEPTH_M, 0.08), floor_mat, "room"
    )
    base.add_box(
        "LeftWall",
        (-ROOM_WIDTH_M / 2.0 - WALL_THICKNESS_M / 2.0, 0.0, ROOM_HEIGHT_M / 2.0),
        (WALL_THICKNESS_M, ROOM_DEPTH_M, ROOM_HEIGHT_M), wall_mat, "room"
    )
    base.add_box(
        "RightWall",
        (ROOM_WIDTH_M / 2.0 + WALL_THICKNESS_M / 2.0, 0.0, ROOM_HEIGHT_M / 2.0),
        (WALL_THICKNESS_M, ROOM_DEPTH_M, ROOM_HEIGHT_M), wall_mat, "room"
    )
    base.add_box(
        "Ceiling", (0.0, 0.0, ROOM_HEIGHT_M + 0.04),
        (ROOM_WIDTH_M, ROOM_DEPTH_M, 0.08), wall_mat, "room"
    )

    opening_width = TARGET_WIDTH_M + 2.0 * FRAME_WIDTH_M
    opening_bottom = WINDOW_BOTTOM_M - FRAME_WIDTH_M
    opening_top = WINDOW_BOTTOM_M + TARGET_HEIGHT_M + FRAME_WIDTH_M

    base.add_box(
        "BackWall_Below", (0.0, BACK_WALL_Y, opening_bottom / 2.0),
        (ROOM_WIDTH_M, WALL_THICKNESS_M, opening_bottom), wall_mat, "room"
    )
    upper_height = ROOM_HEIGHT_M - opening_top
    base.add_box(
        "BackWall_Above",
        (0.0, BACK_WALL_Y, opening_top + upper_height / 2.0),
        (ROOM_WIDTH_M, WALL_THICKNESS_M, upper_height), wall_mat, "room"
    )
    side_width = (ROOM_WIDTH_M - opening_width) / 2.0
    middle_height = opening_top - opening_bottom
    for side, x in (
        ("Left", -(opening_width / 2.0 + side_width / 2.0)),
        ("Right", opening_width / 2.0 + side_width / 2.0),
    ):
        base.add_box(
            f"BackWall_{side}", (x, BACK_WALL_Y, opening_bottom + middle_height / 2.0),
            (side_width, WALL_THICKNESS_M, middle_height), wall_mat, "room"
        )

    frame_z = WINDOW_CENTER_Z_M
    frame_height = TARGET_HEIGHT_M + 2.0 * FRAME_WIDTH_M
    for side, x in (
        ("Left", -(TARGET_WIDTH_M / 2.0 + FRAME_WIDTH_M / 2.0)),
        ("Right", TARGET_WIDTH_M / 2.0 + FRAME_WIDTH_M / 2.0),
    ):
        base.add_box(
            f"WindowFrame_{side}",
            (x, GLASS_Y - FRAME_DEPTH_M / 2.0, frame_z),
            (FRAME_WIDTH_M, FRAME_DEPTH_M, frame_height),
            frame_mat, "window_frame"
        )
    for side, z in (
        ("Bottom", WINDOW_BOTTOM_M - FRAME_WIDTH_M / 2.0),
        ("Top", WINDOW_BOTTOM_M + TARGET_HEIGHT_M + FRAME_WIDTH_M / 2.0),
    ):
        base.add_box(
            f"WindowFrame_{side}",
            (0.0, GLASS_Y - FRAME_DEPTH_M / 2.0, z),
            (TARGET_WIDTH_M, FRAME_DEPTH_M, FRAME_WIDTH_M),
            frame_mat, "window_frame"
        )
    base.add_box(
        "WindowFrame_CenterMullion",
        (0.0, GLASS_Y - FRAME_DEPTH_M / 2.0, frame_z),
        (CENTER_MULLION_M, FRAME_DEPTH_M, TARGET_HEIGHT_M),
        frame_mat, "window_frame"
    )

    pane_center_x = CENTER_MULLION_M / 2.0 + PANE_WIDTH_M / 2.0
    panes = []
    for side, x in (("left", -pane_center_x), ("right", pane_center_x)):
        pane = base.add_box(
            f"StickerTargetGlass_{side}_286x600mm",
            (x, GLASS_Y, WINDOW_CENTER_Z_M),
            (PANE_WIDTH_M, 0.008, TARGET_HEIGHT_M),
            glass_mat, f"glass_target_{side}"
        )
        panes.append(pane)
    return panes, glass_mat


def render_mask(scene, camera, panes, glass_material, stem):
    renderable = [obj for obj in scene.objects if obj.type in {"MESH", "LIGHT"}]
    visibility = {obj.name: obj.hide_render for obj in renderable}
    original_world_use_nodes = scene.world.use_nodes
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    original_background_color = tuple(background.inputs["Color"].default_value)
    original_background_strength = background.inputs["Strength"].default_value
    original_transform = scene.view_settings.view_transform
    original_look = scene.view_settings.look

    pane_set = set(panes)
    for obj in renderable:
        obj.hide_render = obj not in pane_set
    mask_mat = base.emission_material("Mask_White_Emission")
    for pane in panes:
        pane.data.materials.clear()
        pane.data.materials.append(mask_mat)
    background.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    background.inputs["Strength"].default_value = 0.0
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "BW"
    scene.render.image_settings.color_depth = "8"
    scene.render.filepath = str(OUTPUT_DIR / f"{stem}_double_glass_mask.png")
    scene.camera = camera
    bpy.ops.render.render(write_still=True)

    for pane in panes:
        pane.data.materials.clear()
        pane.data.materials.append(glass_material)
    for obj in renderable:
        obj.hide_render = visibility[obj.name]
    background.inputs["Color"].default_value = original_background_color
    background.inputs["Strength"].default_value = original_background_strength
    scene.world.use_nodes = original_world_use_nodes
    scene.view_settings.view_transform = original_transform
    scene.view_settings.look = original_look


def project(scene, camera, point):
    ndc = world_to_camera_view(scene, camera, Vector(point))
    return {
        "x": round(float(ndc.x) * IMAGE_WIDTH_PX, 3),
        "y": round((1.0 - float(ndc.y)) * IMAGE_HEIGHT_PX, 3),
        "normalized_x": round(float(ndc.x), 8),
        "normalized_y": round(1.0 - float(ndc.y), 8),
        "depth_camera_space_m": round(float(ndc.z), 8),
    }


def corners(x_min, x_max):
    front_y = GLASS_Y - 0.0041
    return {
        "top_left": (x_min, front_y, WINDOW_BOTTOM_M + TARGET_HEIGHT_M),
        "top_right": (x_max, front_y, WINDOW_BOTTOM_M + TARGET_HEIGHT_M),
        "bottom_right": (x_max, front_y, WINDOW_BOTTOM_M),
        "bottom_left": (x_min, front_y, WINDOW_BOTTOM_M),
    }


def vector_list(value):
    return [round(float(component), 8) for component in value]


def main():
    base.reset_scene()
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = IMAGE_WIDTH_PX
    scene.render.resolution_y = IMAGE_HEIGHT_PX
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.quality = 95
    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.world.color = (0.055, 0.055, 0.055)

    panes, glass_material = build_room_and_double_window()
    base.add_area_light(
        "KeyLight", (-1.2, -1.5, 2.45), 850.0, 3.0, (1.0, 0.93, 0.84)
    )
    base.add_area_light(
        "FillLight", (1.7, 0.1, 2.15), 520.0, 2.2, (0.78, 0.88, 1.0)
    )

    camera_specs = [
        ("front", 0.0),
        ("left_10deg", 10.0),
        ("right_10deg", -10.0),
    ]
    cameras = [(stem, create_camera(f"Camera_{stem}", angle)) for stem, angle in camera_specs]

    left_bounds = (-TARGET_WIDTH_M / 2.0, -CENTER_MULLION_M / 2.0)
    right_bounds = (CENTER_MULLION_M / 2.0, TARGET_WIDTH_M / 2.0)
    envelope_corners = corners(-TARGET_WIDTH_M / 2.0, TARGET_WIDTH_M / 2.0)
    left_corners = corners(*left_bounds)
    right_corners = corners(*right_bounds)

    metadata = {
        "schema_version": "1.0",
        "interpretation": (
            "The complete two-pane sticker envelope is 600 x 600 mm. "
            "A 28 mm center mullion divides it into two 286 x 600 mm glass panes."
        ),
        "units": {"world": "meter", "physical_dimensions": "millimeter", "image": "pixel"},
        "room": {"width_mm": 4000, "depth_mm": 5000, "height_mm": 2800},
        "double_window": {
            "overall_target_width_mm": 600,
            "overall_target_height_mm": 600,
            "bottom_from_floor_mm": 850,
            "center_mullion_width_mm": 28,
            "left_pane_width_mm": 286,
            "right_pane_width_mm": 286,
            "pane_height_mm": 600,
        },
        "render": {
            "width_px": IMAGE_WIDTH_PX,
            "height_px": IMAGE_HEIGHT_PX,
            "pixel_origin": "top-left",
        },
        "corner_order": ["top_left", "top_right", "bottom_right", "bottom_left"],
        "cameras": {},
    }

    def corner_payload(scene_camera, corner_map):
        return {
            name: {
                "world_m": vector_list(position),
                "pixel": project(scene, scene_camera, position),
            }
            for name, position in corner_map.items()
        }

    for stem, camera in cameras:
        scene.camera = camera
        scene.render.image_settings.file_format = "JPEG"
        scene.render.image_settings.color_mode = "RGB"
        scene.render.filepath = str(OUTPUT_DIR / f"{stem}_double_clay.jpg")
        bpy.ops.render.render(write_still=True)
        render_mask(scene, camera, panes, glass_material, stem)
        metadata["cameras"][stem] = {
            "horizontal_angle_deg": float(camera["horizontal_angle_deg"]),
            "focal_length_mm": camera.data.lens,
            "sensor_width_mm": camera.data.sensor_width,
            "location_world_m": vector_list(camera.location),
            "rotation_euler_radians": vector_list(camera.rotation_euler),
            "clay_image": f"{stem}_double_clay.jpg",
            "combined_glass_mask": f"{stem}_double_glass_mask.png",
            "overall_600x600_corners": corner_payload(camera, envelope_corners),
            "left_pane_corners": corner_payload(camera, left_corners),
            "right_pane_corners": corner_payload(camera, right_corners),
        }

    with open(OUTPUT_DIR / "double_window_corners.json", "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)

    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.color_mode = "RGB"
    scene.camera = cameras[0][1]
    bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT_DIR / "double_window_600x600.blend"))
    print(f"Generated double-window scene in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
