"""
    Whitebox use simulate_env for transition model to predict agent's a' and s' and then compute KL divergence

    e,w,s 是同时更新的，看下step function，和邮件，然后调调hyperparameter看看有没有效果，现在的训练效果不是很好
    argument could be adjust
    learning rate
    max_expisode_timestep
    save_Rate
    max_episode
    Memory_capacity
"""

import argparse
import numpy as np
import tensorflow as tf
import time
import pickle
import copy

"""
import wandb
wandb.init(name='Counterfactual - PPO_Whitebox - all', project="counterfactual_based_attack")
"""




import maddpg.common.tf_util as U
from maddpg.trainer.maddpg import MADDPGAgentTrainer
import tensorflow.contrib.layers as layers

from tensorflow.keras.layers import LSTM, Dense, Input
from tensorflow.keras.models import Model

from PPO import PPO

from scipy.special import softmax
from scipy.special import kl_div


# global variables for PPO

#####################  hyper parameters  ####################



GAMMA = 0.9
A_LR = 0.0001
C_LR = 0.0002
BATCH = 25
A_UPDATE_STEPS = 5
C_UPDATE_STEPS = 5
S_DIM, A_DIM = 15, 5
METHOD = [
    dict(name='kl_pen', kl_target=0.01, lam=0.5),   # KL penalty
    dict(name='clip', epsilon=0.2),                 # Clipped surrogate objective, find this is better
][1]        # choose the method for optimization


###############################  PPO  ####################################



def parse_args():
    parser = argparse.ArgumentParser("Reinforcement Learning experiments for multiagent environments")
    # Environment
    parser.add_argument("--scenario", type=str, default="simple_adversary", help="name of the scenario script")
    parser.add_argument("--max-episode-len", type=int, default=25, help="maximum episode length")
    parser.add_argument("--num-episodes", type=int, default=10000, help="number of episodes")
    parser.add_argument("--num-adversaries", type=int, default=0, help="number of adversaries")
    parser.add_argument("--good-policy", type=str, default="maddpg", help="policy for good agents")
    parser.add_argument("--adv-policy", type=str, default="maddpg", help="policy of adversaries")
    # Core training parameters
    parser.add_argument("--lr", type=float, default=1e-2, help="learning rate for Adam optimizer")
    parser.add_argument("--gamma", type=float, default=0.95, help="discount factor")
    parser.add_argument("--batch-size", type=int, default=1024, help="number of episodes to optimize at the same time")
    parser.add_argument("--num-units", type=int, default=64, help="number of units in the mlp")
    # Checkpointing
    parser.add_argument("--exp-name", type=str, default=None, help="name of the experiment")
    parser.add_argument("--save-dir", type=str, default="./weights_save/", help="directory in which training state and model should be saved")
    parser.add_argument("--save-rate", type=int, default=500, help="save model once every time this many episodes are completed") #originally 1000
    parser.add_argument("--load-dir", type=str, default="./weights_final/", help="directory in which training state and model are loaded")
    # Evaluation
    parser.add_argument("--restore", action="store_true", default=True)
    parser.add_argument("--display", action="store_true", default=False)
    parser.add_argument("--benchmark", action="store_true", default=False)
    parser.add_argument("--benchmark-iters", type=int, default=100000, help="number of iterations run for benchmarking")
    parser.add_argument("--benchmark-dir", type=str, default="./benchmark_files/", help="directory where benchmark data is saved")
    parser.add_argument("--plots-dir", type=str, default="./learning_curves/", help="directory where plot data is saved")
    return parser.parse_args()

def mlp_model(input, num_outputs, scope, reuse=False, num_units=64, rnn_cell=None):
    # This model takes as input an observation and returns values of all actions
    with tf.variable_scope(scope, reuse=reuse):
        out = input
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_outputs, activation_fn=None)
        return out

def kl_divergence(p, q):
    epsilon = 0.0000001
    p = p + epsilon
    q = q + epsilon
    ans = sum(p[i] * np.log(p[i]/q[i]) for i in range(len(p)))
    return ans

"""
def make_env(scenario_name, arglist, benchmark=False):
    from multiagent.environment import MultiAgentEnv
    import multiagent.scenarios as scenarios

    # load scenario from script
    scenario = scenarios.load(scenario_name + ".py").Scenario()
    # create world
    world = scenario.make_world()
    # create multiagent environment
    if benchmark:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, scenario.benchmark_data)
    else:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation)
    return env
"""
def make_env(scenario_name, arglist, benchmark=False, return_ws=False):
    from multiagent.environment import MultiAgentEnv
    import multiagent.scenarios as scenarios

    # load scenario from script
    scenario = scenarios.load(scenario_name + ".py").Scenario()
    # create world
    world = scenario.make_world()
    # create multiagent environment
    if benchmark:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, info_callback = scenario.benchmark_data, copy_callback=scenario.copy_world)
    else:
        env = MultiAgentEnv(world, scenario.reset_world, scenario.reward, scenario.observation, copy_callback = scenario.copy_world)
    
    if not return_ws:
        return env
    else:
        return env, scenario, world

def get_trainers(env, num_adversaries, obs_shape_n, arglist):
    trainers = []
    model = mlp_model
    trainer = MADDPGAgentTrainer
    for i in range(num_adversaries):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.adv_policy=='ddpg')))
    for i in range(num_adversaries, env.n):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.good_policy=='ddpg')))
    return trainers

def train(arglist):
    with U.single_threaded_session():

        # Create environment
        # env = make_env(arglist.scenario, arglist, arglist.benchmark)
        # simulator_env = make_env(arglist.scenario, arglist, arglist.benchmark)
        env, og_scenario, og_world = make_env(arglist.scenario, arglist, return_ws=True, benchmark=False)
        e, s, w = make_env(arglist.scenario, arglist, return_ws=True, benchmark=False)
        

        U.initialize()



        # Create agent trainers
        obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
        num_adversaries = min(env.n, arglist.num_adversaries)
        trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist)
        print('Using good policy {} and adv policy {}'.format(arglist.good_policy, arglist.adv_policy))
        
        
        # Initialize
        U.initialize()
        # Load previous results, if necessary
        if arglist.load_dir == "":
            arglist.load_dir = arglist.save_dir
        if arglist.display or arglist.restore or arglist.benchmark:
            print('Loading previous state...')
            U.load_state(arglist.load_dir)

        episode_rewards = [0.0]  # sum of rewards for all agents
        agent_rewards = [[0.0] for _ in range(env.n)]  # individual agent reward
        final_ep_rewards = []  # sum of rewards for training curve
        final_ep_ag_rewards = []  # agent rewards for training curve
        agent_info = [[[]]]  # placeholder for benchmarking info
        saver = tf.train.Saver()
        obs_n = env.reset()
        
        ppo = PPO() #PPO policy here

        episode_step = 0
        train_step = 0
        t_start = time.time()

        

        PPO_obs = obs_n[2]  # (10,)
        PPO_obs = np.append(PPO_obs, np.zeros((5,)))
        PPO_rew = 0
        PPO_ep_rw = 0

        reward_good = [0.0]
        buffer_s, buffer_a, buffer_r = [], [], []

        print('Starting iterations...')
        while True:
            # get action
            action_n = [agent.action(obs) for agent, obs in zip(trainers,obs_n)]   # (3,5), three agent, each with a probability distribution of action #NONE, UP, DOWN, LEFT, RIGHT
            # environment step

            
            PPO_act = ppo.choose_action(PPO_obs)
            
            ###### modified MADDPG env with output of DDPG network #####
            """
            random = []
            for i in range(5):
                random.append(np.random.rand())
            random = np.array(random)
            random = random / np.sum(random)
            """


            PPO_act = softmax(PPO_act)
            attack_action_n = copy.deepcopy(action_n)
            attack_action_n[2] = PPO_act  # compromised agent attack

            # attack_action_n[2] = random


            # Question 1: how to copy the simuator env
            # MADDPG_new_obs_n, _, _, _ =simulator_env.step(action_n) 
            copied_world = copy.deepcopy(og_world)
            s.copy_world(w, copied_world)
            MADDPG_new_obs_n, _, _, _ = e.step(action_n)
            new_obs_n, rew_n, done_n, info_n = env.step(attack_action_n)

            reward_good[-1] += (rew_n[1] + rew_n[2])
            # new_obs_n, rew_n, done_n, info_n = env.step(action_n)

            # a_t a'_t 
            # s_t+1, s'_t+1    use model 
            # a_t+1, a'_t+1
            
            # Question 2: can I query the policy network without interfering it's transition

            MADDPG_next_action_n = [agent.action(obs) for agent, obs in zip(trainers, MADDPG_new_obs_n)]
            MADDPG_next_action_n_atk = [agent.action(obs) for agent, obs in zip(trainers, new_obs_n)]

            # KL_0 = sum(kl_div(MADDPG_next_action_n[0], MADDPG_next_action_n_atk[0]))
            KL_1 = kl_divergence(MADDPG_next_action_n[1], MADDPG_next_action_n_atk[1])


            #***************************************************************

            PPO_rew = KL_1 * 10
            if train_step % (arglist.max_episode_len - 1) == 0:
                PPO_rew += 80 * -reward_good[-1]


            PPO_new_obs = np.append(new_obs_n[2], attack_action_n[1])
            buffer_s.append(PPO_obs)
            buffer_a.append(PPO_act)
            # buffer_r.append((r+8)/8)    # normalize reward, find to be useful
            buffer_r.append(PPO_rew)

            episode_step += 1
            done = all(done_n)
            terminal = (episode_step >= arglist.max_episode_len)
            # collect experience
            for i, agent in enumerate(trainers):
                agent.experience(obs_n[i], action_n[i], rew_n[i], new_obs_n[i], done_n[i], terminal)
            obs_n = new_obs_n

            
            PPO_obs = PPO_new_obs
            PPO_ep_rw += PPO_rew 

            if (episode_step+1) % BATCH == 0 or episode_step == arglist.max_episode_len - 1:
                v_s_ = ppo.get_v(PPO_new_obs)
                discounted_r = []
                for r in buffer_r[::-1]:
                    v_s_ = r + GAMMA * v_s_
                    discounted_r.append(v_s_)
                discounted_r.reverse()

                bs, ba, br = np.vstack(buffer_s), np.vstack(buffer_a), np.array(discounted_r)[:, np.newaxis]
                buffer_s, buffer_a, buffer_r = [], [], []
                ppo.update(bs, ba, br)
            

            for i, rew in enumerate(rew_n):
                episode_rewards[-1] += rew
                agent_rewards[i][-1] += rew

            

            if done or terminal:
                obs_n = env.reset()
                episode_step = 0
                episode_rewards.append(0)
                reward_good.append(0)
                for a in agent_rewards:
                    a.append(0)
                agent_info.append([[]])
                buffer_s, buffer_a, buffer_r = [], [], []

            # increment global step counter
            train_step += 1

            # for benchmarking learned policies
            if arglist.benchmark:
                for i, info in enumerate(info_n):
                    agent_info[-1][i].append(info_n['n'])
                if train_step > arglist.benchmark_iters and (done or terminal):
                    file_name = arglist.benchmark_dir + arglist.exp_name + '.pkl'
                    print('Finished benchmarking, now saving...')
                    with open(file_name, 'wb') as fp:
                        pickle.dump(agent_info[:-1], fp)
                    break
                continue

            # for displaying learned policies
            if arglist.display:
                time.sleep(0.1)
                env.render()
                continue

            # update all trainers, if not in display or benchmark mode
            loss = None
            for agent in trainers:
                agent.preupdate()
            for agent in trainers:
                loss = agent.update(trainers, train_step)

            # save model, display training output
            if terminal and (len(episode_rewards) % arglist.save_rate == 0):
                
                print("episode reward for PPO agent: {}".format(PPO_ep_rw/arglist.save_rate))
                #wandb.log({'Reward for PPO': PPO_ep_rw/arglist.save_rate})
                PPO_ep_rw = 0
                
                U.save_state(arglist.save_dir, saver=saver)
                # print statement depends on whether or not there are adversaries
                if num_adversaries == 0:
                    print("steps: {}, episodes: {}, mean episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-arglist.save_rate:]), round(time.time()-t_start, 3)))
                    #wandb.log({'Reward for MADDPG': np.mean(episode_rewards[-arglist.save_rate:])})
                    # print("episode reward for DDPG agent: {}".format(DDPG_ep_rw/len(episode_rewards)))
                    print("good reward is {}".format(np.mean(reward_good[-arglist.save_rate:])))
                    #wandb.log({'Reward for Good Agents': np.mean(reward_good[-arglist.save_rate:])})
                else:
                    print("steps: {}, episodes: {}, mean episode reward: {}, agent episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-arglist.save_rate:]),
                        [np.mean(rew[-arglist.save_rate:]) for rew in agent_rewards], round(time.time()-t_start, 3)))
                t_start = time.time()
                # Keep track of final episode reward
                final_ep_rewards.append(np.mean(episode_rewards[-arglist.save_rate:]))
                for rew in agent_rewards:
                    final_ep_ag_rewards.append(np.mean(rew[-arglist.save_rate:]))

            # saves final episode reward for plotting training curve later
            if len(episode_rewards) > arglist.num_episodes:
                rew_file_name = arglist.plots_dir + arglist.exp_name + '_rewards.pkl'
                with open(rew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_rewards, fp)
                agrew_file_name = arglist.plots_dir + arglist.exp_name + '_agrewards.pkl'
                with open(agrew_file_name, 'wb') as fp:
                    pickle.dump(final_ep_ag_rewards, fp)
                print('...Finished total of {} episodes.'.format(len(episode_rewards)))
                break

if __name__ == '__main__':
    arglist = parse_args()
    train(arglist)
