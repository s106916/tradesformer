# %%
import logging
import glob
import os
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3 import PPO
import gymnasium as gym
from gymnasium import spaces
import torch.nn.functional as F
import torch
import torch.nn as nn
from sklearn.preprocessing import MinMaxScaler
import pandas as pd
import numpy as np
from src.util.read_config import EnvConfig
from src.util.rewards import RewardCalculator
from src.util.transaction import TransactionManager
from src.util.plot_chart import TradingChart
from src.util.log_render import render_to_file

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
logger.info(f"Using device: {device}")
ASSET= 'AUDUSD'
env_config_file ='/home/paulg/github/tradesformer/src/configure.json'
cf = EnvConfig(env_config_file)
features = cf.env_parameters("observation_list")
print(features)
# features = ['open', 'high', 'low', 'close', 'vol', 'macd','boll_ub','boll_lb','rsi_30','dx_30','close_30_sma','close_60_sma']

sequence_length = len(features)
# %%

def load_data(csv_file):
    data = pd.read_csv(csv_file)

    
    sequence_length = len(features)  # Number of past observations to consider
    scaler = MinMaxScaler()
    data[features] = scaler.fit_transform(data[features])

    # Check for NaN or Inf values after scaling
    if data[features].isnull().values.any() or np.isinf(data[features].values).any():
        logger.error("Data contains NaN or Inf values after scaling")
        raise ValueError("Data contains NaN or Inf values after scaling")

    # Reset index
    data = data.reset_index()
    logger.info("Data loaded and preprocessed successfully")

    def create_sequences(df, seq_length):
        logger.info("Creating sequences...")
        sequences = []
        for i in range(len(df) - seq_length):
            seq = df.iloc[i:i+seq_length][features].values
            sequences.append(seq)
        logger.info("Sequences created successfully")
        return np.array(sequences)

    sequences = create_sequences(data, sequence_length)
    return data

class TimeSeriesTransformer(nn.Module):
    def __init__(self, input_size, embed_dim, num_heads, num_layers, dropout=0.1):
        super(TimeSeriesTransformer, self).__init__()
        self.model_type = 'Transformer'
        self.embed_dim = embed_dim

        # Embedding layer to project input features to embed_dim dimensions
        self.embedding = nn.Linear(input_size, embed_dim).to(device)

        # Positional encoding parameter
        self.positional_encoding = nn.Parameter(torch.zeros(1, sequence_length, embed_dim).to(device))

        # Transformer encoder layer
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=embed_dim,
            nhead=num_heads,
            dropout=dropout,
            norm_first=True  # Apply LayerNorm before attention and feedforward
        ).to(device)
        self.transformer_encoder = nn.TransformerEncoder(
            encoder_layer,
            num_layers=num_layers,
            norm=nn.LayerNorm(embed_dim).to(device) # Add LayerNorm at the end of the encoder
        )

        # Decoder layer to produce final output
        self.decoder = nn.Linear(embed_dim, embed_dim).to(device)

    def forward(self, src):
        # Apply embedding layer and add positional encoding
        src = self.embedding(src) + self.positional_encoding

        # Pass through the transformer encoder
        output = self.transformer_encoder(src)

        # Pass through the decoder layer
        output = self.decoder(output)

        # Check for NaN or Inf values for debugging
        if torch.isnan(output).any() or torch.isinf(output).any():
            logger.error("Transformer output contains NaN or Inf values")
            raise ValueError("Transformer output contains NaN or Inf values")

        # Return the output from the last time step
        return output[:, -1, :]

class CustomCombinedExtractor(BaseFeaturesExtractor):
    def __init__(self, observation_space: gym.spaces.Box):
        super(CustomCombinedExtractor, self).__init__(observation_space, features_dim=64)
        num_features = observation_space.shape[1]  # Should be 10 in this case

        # Ensure that embed_dim is divisible by num_heads
        embed_dim = 64
        num_heads = 2

        self.layernorm_before = nn.LayerNorm(num_features).to(device) # Added Layer Normalization before transformer

        self.transformer = TimeSeriesTransformer(
            input_size=num_features,
            embed_dim=embed_dim,
            num_heads=num_heads,
            num_layers=2
        ).to(device)

    def forward(self, observations):
        # Apply layer normalization
        normalized_observations = self.layernorm_before(observations.float().to(device)) # Ensure float type

        x = self.transformer(normalized_observations)
        if torch.isnan(x).any() or torch.isinf(x).any():
            logger.error("Invalid values in transformer output")
            raise ValueError("Invalid values in transformer output")
        return x

# %%
class ForexTradingEnv(gym.Env):
    metadata = {'render.modes': ['human']}

    def __init__(self, data, features):
        super(ForexTradingEnv, self).__init__()
        self.data = data
        self.features = features
        self.sequence_length = len(features)
        self.max_steps = len(self.data) - self.sequence_length - 1
        self.current_step = 0
        self.ticket_id = 1
        self.balance_initial = cf.env_parameters("balance")
        self.symbol_col = ASSET
        self.shaping_reward = cf.env_parameters("shaping_reward")
        self.stop_loss = cf.symbol(self.symbol_col, "stop_loss_max")
        self.profit_taken = cf.symbol(self.symbol_col, "profit_taken_max")
        self.point =cf.symbol(self.symbol_col, "profit_taken_max")
        self.transaction_fee=cf.symbol(self.symbol_col, "transaction_fee") 
        self.over_night_penalty =cf.symbol(self.symbol_col, "over_night_penalty")
        self.max_current_holding =cf.symbol(self.symbol_col, "max_current_holding")
        self.good_position_encourage=cf.symbol(self.symbol_col, "good_position_encourage")
        # self.backward_window = self.cf.env_parameters("backward_window")
        self.balance = self.balance_initial
        self.positions = []
        self.total_profit = 0.0
        self.ticket_id = 0
        self.action_space = spaces.Discrete(3)

        # self.reward_calculator = RewardCalculator(
        #     self.data, cf, self.shaping_reward, self.stop_loss, self.profit_taken, self.backward_window
        # )
        # self.transaction_manager = TransactionManager(
        #     cf, self.balance_initial, cf.env_parameters("symbol_col"), self.stop_loss, self.profit_taken
        # )
        
        obs_shape = (self.sequence_length, len(features))
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=obs_shape, dtype=np.float32
        )

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        np.random.seed(seed)

        self.balance = self.balance_initial
        self.positions = []
        self.total_profit = 0.0
        self.ticket_id = 1
        # self.current_step = np.random.randint(self.sequence_length, self.max_steps)
        self.current_step = self.sequence_length
        logger.info(f"Environment reset. Starting at step {self.current_step}")

        observation = self._next_observation()
        info = {}
        return observation, info

    def _next_observation(self):
        obs = self.data.iloc[
            self.current_step - self.sequence_length : self.current_step
        ][features].values
        # Convert to float32 tensor
        obs = torch.tensor(obs, dtype=torch.float32).to(device)
        if torch.isnan(obs).any() or torch.isinf(obs).any():
            logger.error(f"Invalid observation at step {self.current_step}")
            raise ValueError(f"Invalid observation at step {self.current_step}")
        return obs.cpu().numpy() #obs

    def _calculate_reward(self, position):
        '''
        
        '''
        _o, _c, _h, _l,_t,_day = self.data.iloc[self.current_step][["open","close","high","low","time","day"]]
        entry_price = position['ActionPrice']
        direction = position['Type']
        profit_target_price = entry_price + position['PT']/self.point if direction == 'Buy' else entry_price - position['PT']/self.point
        stop_loss_price = entry_price + position['SL']/self.point if direction == 'Buy' else entry_price - position['SL']*self.point

        reward = 0.0


        # Check for stop-loss hit
        if (direction == 'Buy' and _l <= stop_loss_price) or (direction == 'Sell' and _h >= stop_loss_price):
            # loss = abs(current_price - entry_price) * position['size']
            reward = position['SL']  # Negative reward
            position["CloseTime"] = _t
            position["ClosePrice"] = stop_loss_price
            position["Status"] = 1
            position["CloseStep"] = self.current_step
            # logger.info(f"Step {self.current_step}: Reward: {reward}, Close: SL Step: {self.current_step - position['ActionStep']}")    
        # Check for profit target hit
        elif (direction == 'Buy' and _h >= profit_target_price) or (direction == 'Sell' and _l <= profit_target_price):
            # profit = abs(current_price - entry_price) * position['size']
            reward =  position['PT'] # Positive reward
            position["CloseTime"] = _t
            position["ClosePrice"] = profit_target_price
            position["Status"] = 1
            position["CloseStep"] = self.current_step
            # logger.info(f"Step {self.current_step}: Reward: {reward}, Close: PT Step: {self.current_step - position['ActionStep']}")    
        else: 
            if self.current_step + 1 >= len(self.data):
                reward = (_c - position["ActionPrice"] if direction == 'Buy' else position["ActionPrice"] - _c)* self.point
                position["CloseTime"] = _t
                position["ClosePrice"] = _c
                position["Status"] = 2 
                position["CloseStep"] = self.current_step
                logger.info(f"Step {self.current_step}: Reward: {reward}, Close: End Step: {self.current_step - position['ActionStep']}")    
            else:
                delta = _c - position["ActionPrice"]
                if direction == "Buy":
                    reward = self.good_position_encourage if delta >=0 else -self.good_position_encourage
                elif direction == "Sell":
                    reward = -self.good_position_encourage if delta >=0 else self.good_position_encourage
        
        position["Reward"] = position["Reward"] + reward
                        
        return reward

    def step(self, action):
        _o, _c, _h, _l,_t,_day = self.data.iloc[self.current_step][["open","close","high","low","time","day"]]
        reward = 0.0

        # Execute action
        if action in (1, 2):
            self.ticket_id += 1
            position = {
                "Ticket": self.ticket_id,
                "Symbol": self.symbol_col,
                "ActionTime": _t,
                "Type": "Buy" if action ==1 else "Sell",
                "Lot": 1,
                "ActionPrice": _c,
                "SL": self.stop_loss,
                "PT": self.profit_taken,
                "MaxDD": 0,
                "Swap": 0.0,
                "CloseTime": "",
                "ClosePrice": 0.0,
                "Point": 0,
                "Reward": self.transaction_fee,
                "DateDuration": _day,
                "Status": 0,
                "LimitStep": 0,
                "ActionStep":self.current_step,
                "CloseStep":-1,
            }
            self.positions.append(position)                    
            reward = self.transaction_fee #open cost
            # logger.info(f"Step {self.current_step}: Position: {position['Type']} at {position['ActionTime']}")

        # Initialize reward
        
        # Update positions and calculate rewards
        for position in self.positions:
            if position['Status'] == 0:
                position_reward = self._calculate_reward(position)
                reward += position_reward

        self.balance += reward
        # Move to the next time step
        self.current_step += 1

        # Check if episode is done
        done = self.current_step >= self.max_steps or self.balance <= 0

        # Get next observation
        obs = self._next_observation()

        # Convert tensors to CPU for logging or NumPy conversion
        # obs_cpu = obs.cpu().numpy()
        if done: 
            buy = 0
            for position in self.positions:
                if position["Type"] == "Buy":
                    buy +=1
                    
            logger.info(f'Position:{len(self.positions)}/Buy:{buy}---Balance: {self.balance}')
        # Additional info
        info = {}
        truncated = False
        return obs, reward, done, truncated, info

    def render(self, mode='human'):
        logger.info(f'Step: {self.current_step}, Balance: {self.balance:.2f}')

    def render1(self, mode='human', title=None, **kwargs):
        # Render the environment to the screen
        if mode in ('human', 'file'):
            printout = False
            if mode == 'human':
                printout = True
            pm = {
                "log_header": self.log_header,
                "log_filename": self.log_filename,
                "printout": printout,
                "balance": self.balance,
                "balance_initial": self.balance_initial,
                "transaction_close_this_step": self.transaction_close_this_step,
                "done_information": self.done_information
            }
            render_to_file(**pm)
            if self.log_header: self.log_header = False
        elif mode == 'graph' and self.visualization:
            print('plotting...')
            p = TradingChart(self.df, self.transaction_history)
            p.plot()
            

# %%
def single_csv_training(csv_file):
    data = load_data(csv_file)
    
    policy_kwargs = dict(
        features_extractor_class=CustomCombinedExtractor,
        features_extractor_kwargs=dict(),
        net_arch=[dict(pi=[64, 64], vf=[64, 64])],
        activation_fn=nn.ReLU
    )
    env = ForexTradingEnv(data, features)
    model = PPO(
        'MlpPolicy',
        env,
        verbose=1,
        policy_kwargs=policy_kwargs,
        learning_rate=1e-4,  # Reduced learning rate
        max_grad_norm=0.5    # Gradient clipping
    )

    # Train the agent
    logger.info("Starting model training...")
    model.learn(total_timesteps=100000)
    logger.info("Model training complete")

def eval(model_file,env):
    # Evaluate the agent
    model = PPO.load(model_file, env=env)
    observation, info = env.reset()
    done = False

    while not done:
        action, _states = model.predict(observation)
        observation, reward, terminated, truncated, info = env.step(action)
        done = terminated 
        env.render()

    # Save the model
    logger.info("Model saved to 'ppo_forex_transformer'")


def multiply_csv_files_traning(data_directory):
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)
    # Define the directory containing the CSV files
    
    # Get a list of all CSV files in the folder
    csv_files = glob.glob(os.path.join(data_directory, "*.csv"))
    # Set up PPO model parameters
    policy_kwargs = dict(
        features_extractor_class=CustomCombinedExtractor,
        features_extractor_kwargs=dict(),
        net_arch=[dict(pi=[64, 64], vf=[64, 64])],
        activation_fn=nn.ReLU
    )

    # Initialize the batch counter
    batch_num = 1
    model = None
    # Loop through each CSV file for training
    for file in csv_files:
        # Read the CSV file
        data = pd.read_csv(file)
        # Preprocess the data (scaling, etc.)
        # data[features] = scaler.fit_transform(data[features])

        # Reset the environment for the new file
        env = ForexTradingEnv(data, features)


        # Train the model on the current file
        logger.info(f"Starting training on file {file} (Batch {batch_num})")
        model_filename = file.replace("split/", "model/").replace(".csv", ".zip")
        print(model_filename)
        # model_filename = f'/home/paulg/github/tradesformer/data/model/ppo_forex_transformer_batch_{batch_num}.zip'
        if not model :
            # Initial model training
            model = PPO(
                'MlpPolicy',
                env,
                verbose=1,
                policy_kwargs=policy_kwargs,
                learning_rate=1e-4,  # Reduced learning rate
                max_grad_norm=0.5    # Gradient clipping
            )
        model.learn(total_timesteps=10000)  # Adjust the number of timesteps per batch as needed
     
        # Save the model after training on this file
        model.save(model_filename)
        logger.info(f"Model saved as {model_filename}")

        # Increment the batch number for the next file
        batch_num += 1

        # Reload the model if needed, for the next batch (optional, if you want to continue learning)
        model = PPO.load(model_filename, env=env)
        logger.info(f"Model loaded from {model_filename}")
        
    logger.info("Finished training on all files")
