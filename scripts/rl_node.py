#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Point, PoseStamped
from nav_msgs.msg import Odometry
from ackermann_msgs.msg import AckermannDriveStamped
from sensor_msgs.msg import LaserScan
from visualization_msgs.msg import Marker, MarkerArray
from std_msgs.msg import Float32MultiArray
import onnxruntime as ort
import numpy as np
from f1tenth_gym.envs.f110_env import F110Env, Track, Simulator
from f1tenth_gym.envs.base_classes import ScanSimulator2D
import gymnasium as gym
import pathlib
from utils.ros_np_multiarray import to_multiarray_f32
from stable_baselines3 import PPO
from time import time
import utils.utils as utils


class RLNode(Node):

    def __init__(self):
        super().__init__("rl_node")
        self.config = utils.ConfigYAML()
        self.config.load_file(
            "/home/nvidia/f1tenth_ws/src/Learning-Guided-Control-MPPI/config/config_example.yaml"
        )
        if self.config.is_sim:
            odom_topic = "/ego_racecar/odom"
        else:
            odom_topic = "/pf/pose/odom"
        qos = rclpy.qos.QoSProfile(
            history=rclpy.qos.QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=rclpy.qos.QoSReliabilityPolicy.RELIABLE,
            durability=rclpy.qos.QoSDurabilityPolicy.VOLATILE,
        )
        self.drive_publisher = self.create_publisher(
            AckermannDriveStamped, "/drive", qos
        )
        self.pose_subscriber = self.create_subscription(
            Odometry, odom_topic, self.pose_callback, qos
        )
        self.lidar_subscriber = self.create_subscription(
            LaserScan, "/scan", self.laser_callback, qos
        )
        ## publisher for visualizing predicted t+1 pose
        self.viz_publisher = self.create_publisher(MarkerArray, "/viz_rl", qos)
        self.traj_publisher = self.create_publisher(
            Float32MultiArray, "/rl/ref_traj", qos
        )

        self.pose = np.zeros((1, 3), dtype=np.float32)
        self.vels = np.zeros((1, 3), dtype=np.float32)
        self.heading = np.zeros((1, 2), dtype=np.float32)
        self.LF = 0.15875
        self.LR = 0.17145
        N_BEAMS = self.config.num_scans
        self.SCAN_INDEX = np.arange(0, 1080, 1080 // N_BEAMS)
        self.laser_scan = 10 * np.ones((1, N_BEAMS), dtype=np.float32)
        self.model = ort.InferenceSession(
            "/home/nvidia/f1tenth_ws/src/Learning-Guided-Control-MPPI/rl_models/levine_540_4ms_t-025.onnx",
            providers=["CUDAExecutionProvider"]
        )

        self.get_logger().info("model loaded successfully")
        self.CONTROL_MAX = np.array([0.4189, 4.0])
        # create an environment backend for simulating actions to predict future states and lidar scans
        path = "/home/nvidia/f1tenth_ws/src/Learning-Guided-Control-MPPI/waypoints/levine/levine.yaml"
        path = pathlib.Path(path)
        loaded_map = Track.from_track_path(path)
        self.env = gym.make(
            "f1tenth_gym:f1tenth-v0",
            config={
                "map": loaded_map,
                "num_agents": 1,
                "timestep": self.config.rl_sim_time_step,
                "integrator": "rk4",
                "control_input": ["speed", "steering_angle"],
                "model": "st",
                "num_beams": N_BEAMS,
                "observation_config": {"type": "original"},
                "params": F110Env.f1tenth_vehicle_params(),
                "reset_config": {"type": "map_random_static"},
                "scale": 1.0,
            },
            render_mode="rgb_array",
        )
        # store the base occupancy grid for manipulation when receiving a scan
        self.base_occupancy = loaded_map.occupancy_map.copy()
        # number of future states to predict on RL node side
        self.N_SIM = int(
            self.config.n_steps
            * self.config.sim_time_step
            / self.config.rl_sim_time_step
        )
        self.DRIVE = not self.config.use_mppi

    def laser_callback(self, msg: LaserScan):
        min_range = msg.range_min
        max_range = msg.range_max
        ang_min = msg.angle_min
        ang_max = msg.angle_max
        ang_inc = msg.angle_increment
        values = np.array(msg.ranges, dtype=np.float32)
        values = values.clip(min_range, max_range)
        # lidar2world = self.lidar2world(values, np.arange(ang_min, ang_max, ang_inc))
        self.laser_scan[0] = values[self.SCAN_INDEX]

        # print(self.laser_scan.shape)

    def lidar2world(self, lidar, angles):
        lidar2body = np.array(
            [lidar * np.cos(angles), lidar * np.sin(angles)]
        )  # (2, n_scans)
        yaw = self.pose[0, 2]
        R = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
        return R @ lidar2body + np.array([[self.pose[0, 0], self.pose[0, 1]]])

    def pose_callback(self, msg: Odometry):
        # t0 = time()
        ref_traj = np.zeros((self.N_SIM + 1, 7), dtype=np.float32)
        pos = msg.pose.pose.position
        ori = msg.pose.pose.orientation
        lin_vel = msg.twist.twist.linear
        ori_vel = msg.twist.twist.angular
        yaw = 2 * np.arccos(ori.w)
        self.pose[0,0] = pos.x
        self.pose[0,1] = pos.y
        self.pose[0,2] = yaw
        self.vels[0,0] = lin_vel.x
        self.vels[0,1] = lin_vel.y
        self.vels[0,2] = ori_vel.z

        obs = {
            "scan": self.laser_scan,
            "pose": self.pose,
            "vel": self.vels,
            "heading": self.heading,
        }
        # print(obs)
        steer, vel = self.run_model(obs)
        self.heading[0,0] = steer
        self.heading[0,1] = self.get_beta(steer)
        ref_traj[0] = self.to_mppi_state(
            pos.x, pos.y, yaw, ori_vel.z, lin_vel.x, lin_vel.y, steer
        )
        if self.DRIVE:
            drive = AckermannDriveStamped()
            drive.drive.speed = vel
            drive.drive.steering_angle = steer
            self.drive_publisher.publish(drive)
            return
        
        xs = []
        ys = []
        yaws = []
        self.env.reset(options={"poses": np.array([[pos.x, pos.y, yaw]])})
        for i in range(self.N_SIM):
            obs, _, _, _, _ = self.env.step(np.array([[steer, vel]]))
            scan = obs["scans"][:, self.SCAN_INDEX]
            x = float(obs["poses_x"][0])
            y = float(obs["poses_y"][0])
            yaw = float(obs["poses_theta"][0])
            vel_x = float(obs["linear_vels_x"][0])
            vel_y = float(obs["linear_vels_y"][0])
            vel_ori = float(obs["ang_vels_z"][0])
            xs.append(x)
            ys.append(y)
            yaws.append(yaw)
            beta = self.get_beta(steer)
            obs = {'scan': scan,
                   'pose' : np.array([[x, y, yaw]], dtype=np.float32),
                   'vel' : np.array([[vel_x, vel_y, vel_ori]], dtype=np.float32),
                   'heading' : np.array([[steer, beta]], dtype=np.float32)
            }
            steer, vel = self.run_model(obs)
            ref_traj[i + 1] = self.to_mppi_state(
                x, y, yaw, vel_ori, vel_x, vel_y, steer
            )
        self.visualize_future_pose(xs, ys, yaws)
        ref_traj = ref_traj[
            :: int(self.config.sim_time_step / self.config.rl_sim_time_step)
        ]
        # assert ref_traj.shape[0] == self.config.n_steps + 1
        self.traj_publisher.publish(to_multiarray_f32(ref_traj))

    def publish_traj(self, traj: np.ndarray):
        traj = traj.flatten()
        msg = Float32MultiArray()
        msg.data = traj.tolist()
        self.traj_publisher.publish(msg)

    def run_model(self, obs):
        control = self.model.run(None, obs)

        control = control[0][0, 0]
        control = control.clip(-1.0, 1.0) * self.CONTROL_MAX
        steer = float(control[0])
        vel = float(control[1])
        return steer, vel

    def get_beta(self, steer):
        return np.arctan(self.LR * np.tan(steer) / (self.LF + self.LR))

    def to_mppi_state(self, x, y, yaw, yaw_rate, vx, vy, delta):
        return np.array(
            [
                x,
                y,
                delta,
                vx,
                (yaw + np.pi) % (2 * np.pi) - np.pi,
                yaw_rate,
                np.arctan2(vy, vx),
            ]
        )

    def visualize_future_pose(self, xs, ys, yaws):
        msg = MarkerArray()
        for i, x in enumerate(xs):
            marker = Marker()
            marker.pose.position.x = float(x)
            marker.pose.position.y = float(ys[i])
            pose_yaw = float(yaws[i])
            marker.pose.orientation.w = np.cos(pose_yaw / 2)
            marker.pose.orientation.z = np.sin(pose_yaw / 2)
            marker.color.a = marker.color.r = 1.0
            marker.color.b = marker.color.g = 0.0
            marker.header.frame_id = "map"
            marker.ns = "poses"
            marker.scale.x = 0.4
            marker.scale.y = 0.075
            marker.scale.z = 0.05
            marker.id = len(msg.markers)
            marker.type = Marker.ARROW
            marker.action = Marker.ADD
            msg.markers.append(marker)
        self.viz_publisher.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    rl_node = RLNode()

    try:
        rclpy.spin(rl_node)
    finally:
        rl_node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
