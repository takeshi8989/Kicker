import os
import torch
import math
import random
import numpy as np
import genesis as gs
from genesis.utils.geom import quat_to_xyz, transform_by_quat, inv_quat, transform_quat_by_quat


def gs_rand_float(lower, upper, shape, device):
    return (upper - lower) * torch.rand(size=shape, device=device) + lower


def run_sim(env, policy):
    obs, _ = env.reset()
    with torch.no_grad():
        while True:
            actions = policy(obs)
            obs, _, rews, dones, infos = env.step(actions)


class KickerEnv:
    def __init__(self, num_envs, env_cfg, obs_cfg, reward_cfg, command_cfg, show_viewer=False, device="mps", model_path="../model/g1.xml"):
        self.device = torch.device(device)
        self.ball_radius = 0.1
        self.ball_position = torch.tensor([[0.2, -0.2, self.ball_radius]], device=self.device).cpu().numpy()
        self.target_size = (0.01, 1.0, 1.0)
        self.target_distance = 0.5

        self.num_envs = num_envs
        self.num_obs = obs_cfg["num_obs"]
        self.num_privileged_obs = None
        self.num_actions = env_cfg["num_actions"]
        self.num_commands = command_cfg["num_commands"]

        self.simulate_action_latency = True  # there is a 1 step latency on real robot
        self.dt = 0.02  # control frequence on real robot is 50hz
        self.max_episode_length = math.ceil(env_cfg["episode_length_s"] / self.dt)

        self.env_cfg = env_cfg
        self.obs_cfg = obs_cfg
        self.reward_cfg = reward_cfg
        self.command_cfg = command_cfg

        self.obs_scales = obs_cfg["obs_scales"]
        self.reward_scales = reward_cfg["reward_scales"]

        # create scene
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=self.dt, substeps=2),
            viewer_options=gs.options.ViewerOptions(
                res=(1280, 960),
                max_FPS=int(0.5 / self.dt),
                camera_pos=(6, 3, 5),
                camera_lookat=(-2.0, 3.0, 0.8),
                camera_fov=60,
            ),
            vis_options=gs.options.VisOptions(
                show_world_frame=False,
                world_frame_size=1.0,
                n_rendered_envs=1
            ),
            rigid_options=gs.options.RigidOptions(
                dt=self.dt,
                constraint_solver=gs.constraint_solver.Newton,
                enable_collision=True,
                enable_joint_limit=True,
            ),
            show_viewer=show_viewer,
        )

        # add plain
        self.plane = self.scene.add_entity(gs.morphs.URDF(file="urdf/plane/plane.urdf", fixed=True))
        self.plane.geoms[0].set_friction(2.0)

        # add robot
        self.base_init_pos = torch.tensor(self.env_cfg["base_init_pos"], device=self.device)
        self.base_init_quat = torch.tensor(self.env_cfg["base_init_quat"], device=self.device)
        self.inv_base_init_quat = inv_quat(self.base_init_quat)

        current_dir = os.path.dirname(__file__)
        robot_path = os.path.join(current_dir, model_path)
        self.robot = self.scene.add_entity(
            gs.morphs.MJCF(
                file=robot_path
            ),
        )

        self.ball = self.scene.add_entity(
            gs.morphs.Sphere(
                radius=self.ball_radius,
                pos=self.ball_position[0],
                collision=True,
                fixed=False
            ),
        )

        self.target = self.scene.add_entity(
            gs.morphs.Box(
                size=self.target_size,
                pos=(self.target_distance, 0, self.target_size[2] / 2),
                collision=False,
                fixed=True
            ),
        )

        # build
        self.scene.build(n_envs=num_envs)

        # names to indices
        self.motor_dofs = [self.robot.get_joint(name).dof_idx_local for name in self.env_cfg["dof_names"]]

        # PD control parameters
        self.robot.set_dofs_kp([self.env_cfg["kp"]] * self.num_actions, self.motor_dofs)
        self.robot.set_dofs_kv([self.env_cfg["kd"]] * self.num_actions, self.motor_dofs)

        # force range
        self.robot.set_dofs_force_range(
            lower=np.array([-self.env_cfg["force_range"]] * self.num_actions),
            upper=np.array([self.env_cfg["force_range"]] * self.num_actions),
            dofs_idx_local=self.motor_dofs,
        )

        # prepare reward functions and multiply reward scales by dt
        self.reward_functions, self.episode_sums = dict(), dict()
        for name in self.reward_scales.keys():
            self.reward_scales[name] *= self.dt
            self.reward_functions[name] = getattr(self, "_reward_" + name)
            self.episode_sums[name] = torch.zeros((self.num_envs,), device=self.device, dtype=gs.tc_float)

        # initialize buffers
        self.base_lin_vel = torch.zeros((self.num_envs, 3), device=self.device, dtype=gs.tc_float)
        self.base_ang_vel = torch.zeros((self.num_envs, 3), device=self.device, dtype=gs.tc_float)
        self.projected_gravity = torch.zeros((self.num_envs, 3), device=self.device, dtype=gs.tc_float)
        self.global_gravity = torch.tensor([0.0, 0.0, -10], device=self.device, dtype=gs.tc_float).repeat(
            self.num_envs, 1
        )
        self.obs_buf = torch.zeros((self.num_envs, self.num_obs), device=self.device, dtype=gs.tc_float)
        self.rew_buf = torch.zeros((self.num_envs,), device=self.device, dtype=gs.tc_float)
        self.reset_buf = torch.ones((self.num_envs,), device=self.device, dtype=gs.tc_int)
        self.episode_length_buf = torch.zeros((self.num_envs,), device=self.device, dtype=gs.tc_int)
        self.commands = torch.zeros((self.num_envs, self.num_commands), device=self.device, dtype=gs.tc_float)
        self.commands_scale = torch.tensor(
            [self.obs_scales["lin_vel"], self.obs_scales["lin_vel"], self.obs_scales["ang_vel"]],
            device=self.device,
            dtype=gs.tc_float,
        )
        self.actions = torch.zeros((self.num_envs, self.num_actions), device=self.device, dtype=gs.tc_float)
        self.last_actions = torch.zeros_like(self.actions)
        self.dof_pos = torch.zeros_like(self.actions)
        self.dof_vel = torch.zeros_like(self.actions)
        self.last_dof_vel = torch.zeros_like(self.actions)
        self.base_pos = torch.zeros((self.num_envs, 3), device=self.device, dtype=gs.tc_float)
        self.base_quat = torch.zeros((self.num_envs, 4), device=self.device, dtype=gs.tc_float)
        self.default_dof_pos = torch.tensor(
            [self.env_cfg["default_joint_angles"][name] for name in self.env_cfg["dof_names"]],
            device=self.device,
            dtype=gs.tc_float,
        )
        self.extras = dict()  # extra information for logging

    def setup_sim(self, policy):
        gs.tools.run_in_another_thread(fn=run_sim, args=(self, policy))
        self.scene.viewer.start()

    def _resample_commands(self, envs_idx):
        self.commands[envs_idx, 0] = gs_rand_float(*self.command_cfg["lin_vel_x_range"], (len(envs_idx),), self.device)
        self.commands[envs_idx, 1] = gs_rand_float(*self.command_cfg["lin_vel_y_range"], (len(envs_idx),), self.device)
        self.commands[envs_idx, 2] = gs_rand_float(*self.command_cfg["ang_vel_range"], (len(envs_idx),), self.device)

    def step(self, actions):
        self.actions = torch.clip(actions, -self.env_cfg["clip_actions"], self.env_cfg["clip_actions"])
        exec_actions = self.last_actions if self.simulate_action_latency else self.actions
        target_dof_pos = exec_actions * self.env_cfg["action_scale"] + self.default_dof_pos
        self.robot.control_dofs_position(target_dof_pos, self.motor_dofs)
        self.scene.step()

        # cam_pose = self.scene.viewer.camera_pose
        # # move camera with the robot
        # cam_pose[:3, 3] = self.base_pos[0].cpu().numpy() + np.array([6, 3, 5])
        # # look at the robot base
        # # cam_pose[:3, :3] = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])
        # self.scene.viewer.set_camera_pose(cam_pose)

        # update buffers
        self.episode_length_buf += 1
        self.base_pos[:] = self.robot.get_pos()
        self.base_quat[:] = self.robot.get_quat()
        self.base_euler = quat_to_xyz(
            transform_quat_by_quat(torch.ones_like(self.base_quat) * self.inv_base_init_quat, self.base_quat)
        )
        inv_base_quat = inv_quat(self.base_quat)
        self.base_lin_vel[:] = transform_by_quat(self.robot.get_vel(), inv_base_quat)
        self.base_ang_vel[:] = transform_by_quat(self.robot.get_ang(), inv_base_quat)
        self.projected_gravity = transform_by_quat(self.global_gravity, inv_base_quat)
        self.dof_pos[:] = self.robot.get_dofs_position(self.motor_dofs)
        self.dof_vel[:] = self.robot.get_dofs_velocity(self.motor_dofs)

        # resample commands
        envs_idx = (
            (self.episode_length_buf % int(self.env_cfg["resampling_time_s"] / self.dt) == 0)
            .nonzero(as_tuple=False)
            .flatten()
        )
        self._resample_commands(envs_idx)

        # check termination and reset
        self.reset_buf = self.episode_length_buf > self.max_episode_length
        self.reset_buf |= torch.abs(self.base_euler[:, 1]) > self.env_cfg["termination_if_pitch_greater_than"]
        self.reset_buf |= torch.abs(self.base_euler[:, 0]) > self.env_cfg["termination_if_roll_greater_than"]
        self.reset_buf |= self.base_pos[:, 2] < self.env_cfg.get("termination_base_height", 0.4)

        # self.reset_buf |= self.is_ball_hit_target()

        time_out_idx = (self.episode_length_buf > self.max_episode_length).nonzero(as_tuple=False).flatten()
        self.extras["time_outs"] = torch.zeros_like(self.reset_buf, device=self.device, dtype=gs.tc_float)
        self.extras["time_outs"][time_out_idx] = 1.0

        self.reset_idx(self.reset_buf.nonzero(as_tuple=False).flatten())

        # compute reward
        self.rew_buf[:] = 0.0
        for name, reward_func in self.reward_functions.items():
            rew = reward_func() * self.reward_scales[name]
            self.rew_buf += rew
            self.episode_sums[name] += rew

        # compute observations
        self.obs_buf = torch.cat(
            [
                self.base_ang_vel * self.obs_scales["ang_vel"],  # 3
                self.projected_gravity,  # 3
                self.commands * self.commands_scale,  # 3
                (self.dof_pos - self.default_dof_pos) * self.obs_scales["dof_pos"],  # 12
                self.dof_vel * self.obs_scales["dof_vel"],  # 12
                self.actions,  # 12
                self.robot.get_pos(),  # 3
                self.ball.get_pos(),  # 3
                self.target.get_pos(),  # 3
            ],
            axis=-1,
        )

        self.last_actions[:] = self.actions[:]
        self.last_dof_vel[:] = self.dof_vel[:]

        return self.obs_buf, None, self.rew_buf, self.reset_buf, self.extras

    def get_observations(self):
        return self.obs_buf

    def get_privileged_observations(self):
        return None

    def reset_idx(self, envs_idx):
        if len(envs_idx) == 0:
            return

        # reset dofs
        self.dof_pos[envs_idx] = self.default_dof_pos
        self.dof_vel[envs_idx] = 0.0
        self.robot.set_dofs_position(
            position=self.dof_pos[envs_idx],
            dofs_idx_local=self.motor_dofs,
            zero_velocity=True,
            envs_idx=envs_idx,
        )

        # reset base
        self.base_pos[envs_idx] = self.base_init_pos
        self.base_quat[envs_idx] = self.base_init_quat.reshape(1, -1)
        self.robot.set_pos(self.base_pos[envs_idx], zero_velocity=False, envs_idx=envs_idx)
        self.robot.set_quat(self.base_quat[envs_idx], zero_velocity=False, envs_idx=envs_idx)
        self.base_lin_vel[envs_idx] = 0
        self.base_ang_vel[envs_idx] = 0
        self.robot.zero_all_dofs_velocity(envs_idx)

        # Randomly reset ball position
        self.random_reset()

        # reset buffers
        self.last_actions[envs_idx] = 0.0
        self.last_dof_vel[envs_idx] = 0.0
        self.episode_length_buf[envs_idx] = 0
        self.reset_buf[envs_idx] = True

        # fill extras
        self.extras["episode"] = {}
        for key in self.episode_sums.keys():
            self.extras["episode"]["rew_" + key] = (
                torch.mean(self.episode_sums[key][envs_idx]).item() / self.env_cfg["episode_length_s"]
            )
            self.episode_sums[key][envs_idx] = 0.0

        self._resample_commands(envs_idx)

    def random_reset(self):
        random_positions = []
        for _ in range(self.num_envs):
            # some randomness
            pos = [random.uniform(0.1, 0.11), random.uniform(-0.15, -0.16), self.ball_radius]
            random_positions.append(pos)
        ball_positions = torch.tensor(random_positions, device=self.device).cpu().numpy()
        self.ball.set_pos(ball_positions, envs_idx=torch.arange(self.num_envs))

    def reset(self):
        self.reset_buf[:] = True
        self.reset_idx(torch.arange(self.num_envs, device=self.device))
        return self.obs_buf, None

    def is_ball_hit_target(self):
        ball_pos = self.ball.get_pos()
        target_pos = self.target.get_pos()

        hit_x = (ball_pos[:, 0] - self.ball_radius <= target_pos[:, 0] + self.target_size[0] / 2) & \
                (ball_pos[:, 0] + self.ball_radius >= target_pos[:, 0] - self.target_size[0] / 2)

        hit_y = (ball_pos[:, 1] - self.ball_radius <= target_pos[:, 1] + self.target_size[1] / 2) & \
                (ball_pos[:, 1] + self.ball_radius >= target_pos[:, 1] - self.target_size[1] / 2)

        hit_z = (ball_pos[:, 2] - self.ball_radius <= target_pos[:, 2] + self.target_size[2] / 2) & \
                (ball_pos[:, 2] + self.ball_radius >= target_pos[:, 2] - self.target_size[2] / 2)

        return hit_x & hit_y & hit_z

    # ------------ reward functions----------------
    def _reward_forward_velocity(self):
        # Reward forward velocity (x-axis)
        forward_velocity = self.base_lin_vel[:, 0]
        return forward_velocity

    def _reward_ball_hit_target(self):
        hit = self.is_ball_hit_target()
        # the faster the ball hits the target, the higher the reward
        ball_velocity = torch.norm(self.ball.get_vel(), dim=-1)
        return torch.where(hit, ball_velocity, torch.zeros((self.num_envs,), device=self.device))

    def _reward_ball_distance_from_target(self):
        ball_pos = self.ball.get_pos()
        target_pos = self.target.get_pos()
        ball_distance = torch.norm(ball_pos - target_pos, dim=-1)
        # ball_distance = torch.nan_to_num(ball_distance, nan=10.0)
        return -ball_distance

    def _reward_episode_length(self):
        return -1.0

    def _reward_base_height(self):
        return -self.base_pos[:, 2]

    def _reward_survival_time(self):
        return torch.ones(self.num_envs, device=self.device)

    def _reward_energy_efficiency(self):
        energy_efficiency = torch.sum(torch.square(self.actions), dim=1)
        return -energy_efficiency

    def _reward_stability(self):
        roll_pitch_error = torch.square(self.base_euler[:, 0]) + torch.square(self.base_euler[:, 1])
        return torch.exp(-roll_pitch_error)

    # penalize if both feet are in contact
    def _reward_foot_contact(self):
        left_foot_link_name = "left_ankle_roll_link"
        right_foot_link_name = "right_ankle_roll_link"

        left_foot_entity = self.robot.get_link(left_foot_link_name)
        right_foot_entity = self.robot.get_link(right_foot_link_name)

        left_foot_contacts = self.robot.get_contacts(with_entity=left_foot_entity)
        right_foot_contacts = self.robot.get_contacts(with_entity=right_foot_entity)

        left_foot_in_contact = torch.tensor(left_foot_contacts["valid_mask"], device=self.device).any(dim=1).float()
        right_foot_in_contact = torch.tensor(right_foot_contacts["valid_mask"], device=self.device).any(dim=1).float()

        return -(left_foot_in_contact * right_foot_in_contact)

    def _reward_leg_swing(self):
        # Get the hip and knee joint actions for both legs.
        left_hip = self.actions[:, self.env_cfg["dof_names"].index("left_hip_pitch_joint")]
        right_hip = self.actions[:, self.env_cfg["dof_names"].index("right_hip_pitch_joint")]
        left_knee = self.actions[:, self.env_cfg["dof_names"].index("left_knee_joint")]
        right_knee = self.actions[:, self.env_cfg["dof_names"].index("right_knee_joint")]

        # Encourage symmetrical movement:
        # If the left and right joints move in opposite directions, the sum will be near zero.
        hip_symmetry_penalty = torch.abs(left_hip + right_hip)
        knee_symmetry_penalty = torch.abs(left_knee + right_knee)

        # Optionally, reward overall movement magnitude (i.e., being dynamic).
        hip_magnitude = torch.abs(left_hip) + torch.abs(right_hip)
        knee_magnitude = torch.abs(left_knee) + torch.abs(right_knee)

        # Combine the terms: the idea is to reward high movement magnitude while penalizing asymmetry.
        # You can tune these coefficients as needed.
        reward = hip_magnitude + knee_magnitude - (hip_symmetry_penalty + knee_symmetry_penalty)
        return reward
