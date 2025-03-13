stand = {
    "base_height": 0.2,
    "survival_time": 2.0,
    "energy_efficiency": 1.0,
    "stability": 0.4,
}


step = {
    "base_height": 0.2,
    "survival_time": 1.2,
    "stability": 0.2,
    "foot_contact": 0.5,
    "leg_swing": 0.5,
}


kicker_v1 = {
    "ball_hit_target": 50000.0,
    "base_height": 0.001,
    "survival_time": 0.4,
    "energy_efficiency": 0.001,
    "stability": 0.001,
    "foot_contact": 0.1,
}


def get_reward_scales(exp_name):
    if exp_name == "stand":
        return stand
    elif exp_name == "step":
        return step
    elif exp_name == "kicker_v1":
        return kicker_v1
    else:
        raise ValueError(f"Unknown exp_name: {exp_name}")
