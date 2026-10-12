from __future__ import annotations

import math
import unittest

from app.core.spine_pose import (
    BONE_ACTIVE, BONE_IK_TARGET, SpineBone, animation_offsets, pick_bone, rotate_bone, translate_bone,
)


def bone(index=1, name="arm", parent=0, flags=BONE_ACTIVE, length=10.0, rotation=0.0, x=0.0, y=0.0,
         world_rotation=0.0, world=(0.0, 0.0), scale=(1.0, 1.0)):
    radians = math.radians(world_rotation)
    cos, sin = math.cos(radians), math.sin(radians)
    return SpineBone(index, name, parent, flags, length, x, y, rotation, 1.0, 1.0, 0.0, 0.0,
                     cos * scale[0], -sin * scale[1], sin * scale[0], cos * scale[1], world[0], world[1])


class SpinePoseTests(unittest.TestCase):
    def test_rotation_follows_the_swept_angle_and_flips_under_a_mirrored_parent(self):
        arm = bone(rotation=30.0, world_rotation=30.0, world=(5.0, 5.0))
        parent = bone(index=0, name="root", parent=-1)
        # Grab anywhere: sweeping 90 degrees around the origin adds 90 degrees.
        self.assertAlmostEqual(rotate_bone(arm, parent, (15.0, 5.0), (5.0, 15.0)), 120.0)
        # Sweeping across the +-180 seam takes the short way round.
        self.assertAlmostEqual(rotate_bone(arm, parent, (-5.0, 6.0), (-5.0, 4.0)), 30.0 + 2 * math.degrees(math.atan2(1, 10)))
        mirrored = bone(index=0, name="root", parent=-1, scale=(-1.0, 1.0))
        self.assertAlmostEqual(rotate_bone(arm, mirrored, (15.0, 5.0), (5.0, 15.0)), -60.0)

    def test_translation_is_measured_in_the_parent_space(self):
        parent = bone(index=0, name="root", parent=-1, world_rotation=90.0, world=(100.0, 0.0), scale=(2.0, 2.0))
        child = bone(x=3.0, y=4.0)
        # World +y is the parent's +x axis, at half scale.
        x, y = translate_bone(child, parent, (0.0, 0.0), (0.0, 10.0))
        self.assertAlmostEqual(x, 8.0)
        self.assertAlmostEqual(y, 4.0)
        self.assertEqual(translate_bone(child, None, (0.0, 0.0), (2.0, -1.0)), (5.0, 3.0))
        local = parent.world_to_local(100.0, 4.0)
        self.assertAlmostEqual(local[0], 2.0)
        self.assertAlmostEqual(local[1], 0.0)

    def test_pick_prefers_joints_and_ik_targets_and_ignores_inactive_bones(self):
        identity = lambda x, y: (x, y)  # noqa: E731 - view equals skeleton space here
        long_bone = bone(index=1, name="long", length=100.0, world=(0.0, 0.0))
        joint = bone(index=2, name="joint", length=20.0, world=(50.0, 3.0))
        target = bone(index=3, name="target", flags=BONE_ACTIVE | BONE_IK_TARGET, length=0.0, world=(90.0, 0.0))
        hidden = bone(index=4, name="hidden", flags=0, length=0.0, world=(20.0, 0.0))
        bones = [long_bone, joint, target, hidden]
        self.assertEqual(pick_bone(bones, (50.0, 1.0), identity).name, "joint")
        self.assertEqual(pick_bone(bones, (30.0, 2.0), identity).name, "long")
        self.assertEqual(pick_bone(bones, (98.0, 7.0), identity).name, "target")
        self.assertEqual(pick_bone(bones, (20.0, 0.0), identity).name, "long")
        self.assertIsNone(pick_bone(bones, (30.0, 40.0), identity))

    def test_animation_offsets_are_relative_to_the_setup_pose(self):
        self.assertEqual(animation_offsets({"rotation": 15, "x": 2}, {"rotation": 40.0, "x": 5.0, "y": -1.0}),
                         {"angle": 25.0, "x": 3.0, "y": -1.0})
        self.assertEqual(animation_offsets({}, {"rotation": -10.0}), {"angle": -10.0})


if __name__ == "__main__":
    unittest.main()
