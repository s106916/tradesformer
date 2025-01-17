import os
import mplfinance as mpf
import pandas as pd
import datetime

class TradingChart():
    """An ohlc trading visualization using matplotlib made to render tgym environment"""
    def __init__(self, csv_file, transaction_history, save_plot, **kwargs):
        df= pd.read_csv(csv_file)
        self.save_plot =save_plot
        self.output_file = csv_file.replace("split/", "plot/").replace(".csv", ".png")
        self.ohlc = df[['time','open','high','low','close','symbol']].copy()
        self.ohlc = self.ohlc.rename(columns={'time':'Date','open':'Open','high':'High','low':'Low','close':'Close'})
        self.ohlc.index = pd.DatetimeIndex(self.ohlc['Date'])
        self.transaction_history = transaction_history
        self.parameters = {"figscale":6.0,"style":"nightclouds", "type":"hollow_and_filled", "warn_too_much_data":2000 }
        self.symbol = self.ohlc.iloc[1]["symbol"]
    def transaction_line(self):
        _wlines=[]
        _llines=[]

        rewards = 0
        for tr in self.transaction_history:
            rd = tr['pips']  
            rewards += rd
            if tr['CloseStep'] >= 0 :
                if rd > 0 :
                    _wlines.append([(tr['ActionTime'],tr['ActionPrice']),(tr['CloseTime'],tr['ClosePrice'])])
                else:
                    _llines.append([(tr['ActionTime'],tr['ActionPrice']),(tr['CloseTime'],tr['ClosePrice'])])

        combined_alines = _wlines + _llines
        combined_colors = ['b'] * len(_wlines) + ['r'] * len(_llines)
        return combined_alines, combined_colors, rewards
    
    def plot(self):
        combined_alines, combined_colors, rewards = self.transaction_line()
        title = f'Symbol:{self.symbol}   Rewards:{rewards}'
        # os.makedirs(self.output_file, exist_ok=True)
        if self.save_plot:        
            dir_path = os.path.dirname(self.output_file)
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)
            
            mpf.plot(
                self.ohlc, 
                type='candle', 
                alines = dict(alines=combined_alines, colors=combined_colors),
                title=title,
                savefig=dict(fname=self.output_file, dpi=300, bbox_inches="tight"),
                )
        else:    
            mpf.plot(
                self.ohlc, 
                type='candle', 
                alines = dict(alines=combined_alines, colors=combined_colors),
                title=title,
                )