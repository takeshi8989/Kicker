import os
import argparse
import numpy as np
import genesis as gs


BALL_RADIUS = 0.1
TARGET_SIZE = (0.01, 1.0, 1.0)
TARGET_DISTANCE = 2.0


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=True)
    args = parser.parse_args()

    ########################## init ##########################
    gs.init(backend=gs.cpu)

    ########################## create a scene ##########################

    scene = gs.Scene(
        viewer_options=gs.options.ViewerOptions(
            res=(1280, 960),
            camera_pos=(6, 3, 5),
            camera_lookat=(-2.0, 3.0, 0.8),
            camera_fov=60,
            max_FPS=60,
        ),
        sim_options=gs.options.SimOptions(
            dt=0.01,
            gravity=(0, 0, -9.81),
        ),
        show_viewer=args.vis,
    )

    ########################## entities ##########################
    plane = scene.add_entity(
        gs.morphs.Plane(),
    )

    ball = scene.add_entity(
        gs.morphs.Sphere(
            radius=BALL_RADIUS,
            pos=(0.4, -0.1, BALL_RADIUS),
            collision=True,
            fixed=False
        ),
    )

    target = scene.add_entity(
        gs.morphs.Box(
            size=TARGET_SIZE,
            pos=(TARGET_DISTANCE, 0, TARGET_SIZE[2] / 2),
            collision=False,
            fixed=True
        ),
    )

    # when loading an entity, you can specify its pose in the morph.
    current_dir = os.path.dirname(__file__)
    path = os.path.join(current_dir, '../model/g1.xml')
    robot = scene.add_entity(
        gs.morphs.MJCF(
            file=path,
        ),
    )

    ########################## build ##########################
    scene.build()

    jnt_names = [
        "left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
        "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
        "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
        "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint",
        "waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint",
        "left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
        "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint",
        "right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
        "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint"
    ]
    dofs_idx = [robot.get_joint(name).dof_idx_local for name in jnt_names]

    ############ Optional: set control gains ############
    # set positional gains
    robot.set_dofs_kp(
        kp=np.array([100] * 29),
        dofs_idx_local=dofs_idx,
    )
    # set velocity gains
    robot.set_dofs_kv(
        kv=np.array([45] * 29),
        dofs_idx_local=dofs_idx,
    )
    # set force range for safety
    robot.set_dofs_force_range(
        lower=np.array([-100] * 29),
        upper=np.array([100] * 29),
        dofs_idx_local=dofs_idx,
    )

    gs.tools.run_in_another_thread(fn=run_sim, args=(scene, args.vis, robot, ball, target, dofs_idx))
    if args.vis:
        scene.viewer.start()


def is_ball_hit_target(ball, target):
    ball_pos = ball.get_pos()
    target_pos = target.get_pos()

    if ball_pos[0] - BALL_RADIUS > target_pos[0] + TARGET_SIZE[0] / 2 or ball_pos[0] + BALL_RADIUS < target_pos[0] - TARGET_SIZE[0] / 2:
        return False
    if ball_pos[1] - BALL_RADIUS > target_pos[1] + TARGET_SIZE[1] / 2 or ball_pos[1] + BALL_RADIUS < target_pos[1] - TARGET_SIZE[1] / 2:
        return False
    if ball_pos[2] - BALL_RADIUS > target_pos[2] + TARGET_SIZE[2] / 2 or ball_pos[2] + BALL_RADIUS < target_pos[2] - TARGET_SIZE[2] / 2:
        return False

    print("Ball pos: ", ball_pos)
    return True


def run_sim(scene, enable_vis, robot, ball, target, dofs_idx):
    # PD control
    for i in range(12500):
        if i == 0:
            robot.control_dofs_position(
                np.array([0] * 29),
                dofs_idx,
            )

        scene.step()

        if is_ball_hit_target(ball, target):
            print("Ball hit the target!")
            break

    if enable_vis:
        scene.viewer.stop()


if __name__ == "__main__":
    main()
