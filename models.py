import torch as tr
import numpy as np
import time
from torch.autograd import Variable
from torchinfo import summary
from pytorch_lightning import LightningModule
from torch.utils.data import DataLoader
from torch.utils.data import Dataset


class NormConv(tr.nn.Module):
    def __init__(self, in_channel, out_channel, kernal_size, stride, nums_conv=3):
        super(NormConv, self).__init__()
        self.in_channel = in_channel
        self.out_channel = out_channel
        self.kernal_size = kernal_size
        self.stride = stride
        self.nums_conv = nums_conv
        self.out_channels = [self.out_channel for i in range(self.nums_conv)]
        self.in_channels = [self.in_channel] + self.out_channels[:-1]
        modules_list = []
        pad = int((kernal_size-stride)/2)
        
        for i in range(nums_conv):
            cnn_block = tr.nn.Sequential(
                tr.nn.Conv1d(
                    self.in_channels[i], 
                    self.out_channels[i], 
                    self.kernal_size, 
                    self.stride,
                    padding=pad
                ),
                tr.nn.BatchNorm1d(self.out_channels[i]),
                tr.nn.ReLU(),
                tr.nn.Dropout(0.5)
            )
            modules_list.append(cnn_block)
            
        self.moduleslist = tr.nn.ModuleList(modules_list)

        
    def forward(self, x):
        for _,module in enumerate(self.moduleslist):
            x = module(x)
        return x

class TacoEncoder(tr.nn.Module):
    def __init__(self, embeding_size=512, 
                 kernal_shape={'out_channel':512, 'kernal_size':5, 'stride':1}, 
                 lstm_unit_size=256):
        super(TacoEncoder, self).__init__()

        self.embeding_size = embeding_size
        # channels, kernal_size, stride_len
        self.kernal_shape = kernal_shape
        self.lstm_unit_size = lstm_unit_size 

        self.char_embedding = tr.nn.Embedding(31, self.embeding_size)
        self.conv_layer = NormConv(self.embeding_size, **self.kernal_shape, nums_conv=3)
        self.bi_lstm = tr.nn.LSTM(512,self.lstm_unit_size, bidirectional=True)
        
    def forward(self, x, x_len):
        x = self.char_embedding(x)
        x = x.permute(0,2,1)
        self.conv_layer(x)
        
        x = x.permute(0,2,1)
        x = tr.nn.utils.rnn.pack_padded_sequence(x, x_len, enforce_sorted=False,batch_first=True)
        self.bi_lstm.flatten_parameters()
        x,_ = self.bi_lstm(x)
        x, x_len = tr.nn.utils.rnn.pad_packed_sequence(x,batch_first=True)
        
        return x

class LocationalAttention(tr.nn.Module):
    def __init__(self, enc_dim=512, dec_dim=1024, att_dim=128,
                 conv_filter_dim=32, conv_kernal_size=31, conv_stride=1):
        super(LocationalAttention, self).__init__()
        """
            query: the output of decoder #(batch, dec_dim)
            value: the output of encoder #(batch, time_step, enc_dim)
        """
        self.conv = tr.nn.Conv1d(2, conv_filter_dim, 
                                 conv_kernal_size, conv_stride,
                                padding=int((conv_kernal_size-conv_stride)/2))
        self.location_linear = tr.nn.Linear(conv_filter_dim, att_dim, bias=False)
        self.query_proj = tr.nn.Linear(dec_dim, att_dim, bias=False)
        self.value_proj = tr.nn.Linear(enc_dim, att_dim, bias=False)
        self.score = tr.nn.Linear(att_dim, 1, bias=False)
        
    def get_location_info(self, location_info_cat):
        # location_info_cat (batch_size, 2, time_step)
        location_info = self.conv(location_info_cat) 
        # locaiton_info (batch_size, filter_dim, time_step) -> (batch_size, time_step, filter_dim)
        location_info = location_info.permute(0, 2, 1)
        # location_info (batch_size, time_step, att_dim)
        location_info = self.location_linear(location_info)
        return location_info
    
    def get_info_weights(self, query, value, location_info_cat):
        # query_transformed (batch_size, 1,  att_dim)
        query_transformed = self.query_proj(query)
        # value_transformed (batch_size, time_step, att_dim)
 
        value_transformed = self.value_proj(value)
        # location_info (batch_size, time_step, att_dim)
        location_info = self.get_location_info(location_info_cat)
        # info_weights (batch_size, time_step, 1)
        query_transformed = query_transformed.unsqueeze(1)

        info_weights = self.score(tr.tanh(
            query_transformed + value_transformed + location_info
        ))
        return info_weights, value_transformed
    
    def forward(self, query, value, location_info_cat):
        info_weights, value_transformed = self.get_info_weights(query, value, location_info_cat)
        # att_weights (batch_size, time_step)
        info_weights = info_weights.squeeze()
        att_weights = tr.nn.functional.softmax(info_weights, dim=1)
        # att_context (batch_size, 1, time_step) * (batch_size, time_step, att_dim) 
        #              -> (batch_size, 1, att_dim)
        att_context = tr.bmm(info_weights.unsqueeze(1), value)
        att_context = att_context.squeeze(1)
        return att_context, att_weights

class pre_net(tr.nn.Module):
    def __init__(self, pre_net_in, pre_net_out=256):
        super(pre_net, self).__init__()
        self.fc1 = tr.nn.Linear(pre_net_in, pre_net_out)
        self.fc2 = tr.nn.Linear(pre_net_out, pre_net_out)
        self.seq1 = tr.nn.Sequential(self.fc1, tr.nn.ReLU(), tr.nn.Dropout(0.5))
        self.seq2 = tr.nn.Sequential(self.fc2, tr.nn.ReLU(), tr.nn.Dropout(0.5))
        self.moduleslist = tr.nn.ModuleList([self.seq1, self.seq2])

    def forward(self, x):
        for module in self.moduleslist:
            x = module(x)
        return x
    
class post_net(tr.nn.Module):
    def __init__(self,  mel_dim, post_net_hidden=512, kernal_size=5, stride=1, nums_conv=5):
        super(post_net, self).__init__()
        self.in_channel = mel_dim
        self.out_channel = post_net_hidden
        self.kernal_size = kernal_size
        self.stride = stride
        self.nums_conv = nums_conv
        self.out_channels = [self.out_channel for i in range(self.nums_conv-1)] + [self.in_channel]
        self.in_channels = [self.in_channel] + self.out_channels[:-1]
        modules_list = []
        pad = int((kernal_size-stride)/2)

        for i in range(nums_conv):
            cnn_block = tr.nn.Sequential(
                tr.nn.Conv1d(
                    self.in_channels[i], 
                    self.out_channels[i], 
                    self.kernal_size, 
                    self.stride,
                    padding=pad
                ),
                tr.nn.BatchNorm1d(self.out_channels[i]),
            )
            modules_list.append(cnn_block)
        self.moduleslist = tr.nn.ModuleList(modules_list)

    def forward(self, x):
        for _, module in enumerate(self.moduleslist):
            x = module(x)
            if _ < (len(self.moduleslist)-1):
                x = tr.nn.functional.dropout(tr.tanh(x), p=0.5)
        return x

class TacoDecoder(tr.nn.Module):
    def __init__(self, att_dim=128, enc_dim=512, n_mels=80, frames_per_step=2, prenet_dim=256, lstm_dec_dim=1024, lstm_att_dim=1024):
        super(TacoDecoder, self).__init__()
    
        self.lstm_att_dim = lstm_att_dim
        self.lstm_dec_dim = lstm_dec_dim
        self.prenet_dim = prenet_dim
        self.n_mels = n_mels
        self.frames_per_step = frames_per_step
        self.prenet = pre_net(enc_dim, prenet_dim)
        self.enc_dim = enc_dim
        """
        Lstm_att:
            [enc_embedding, pre_net_output], lstm_att_h, lstm_att_c -> lstm_att_h, lstm_att_c
        Attention:
            the query is from the predecssing lstm.
            enc_embedding, lstm_att_h, [pre_att, pre_att_cum] -> pre_att, context
        Lstm_dec:
            [enc_embedding, pre_att], lstm_dec_h, lstm_dec_c -> stm_dec_h, lstm_dec_c
        
        """
        self.lstm_att = tr.nn.LSTMCell(prenet_dim + enc_dim, lstm_att_dim)
        self.attention = LocationalAttention(enc_dim, lstm_att_dim, att_dim)
        self.lstm_dec = tr.nn.LSTMCell(lstm_att_dim + enc_dim, lstm_dec_dim)
        
        self.mel_proj = tr.nn.Linear(lstm_dec_dim + enc_dim, frames_per_step*n_mels)
        self.stop_proj = tr.nn.Linear(lstm_dec_dim + enc_dim, 1)
    
    def init_parameters(self, value):
        batch_size, max_time, embed_dim = value.shape
        self.value = value
        self.lstm_att_h = Variable(tr.zeros(batch_size, self.lstm_att_dim))
        self.lstm_att_c = Variable(tr.zeros(batch_size, self.lstm_att_dim))
        self.lstm_dec_h = Variable(tr.zeros(batch_size, self.lstm_dec_dim))
        self.lstm_dec_c = Variable(tr.zeros(batch_size, self.lstm_dec_dim))
        
        self.attention_weight_cum = Variable(tr.zeros(batch_size,1, max_time))
        self.attention_weight = Variable(tr.zeros(batch_size,1, max_time))
        self.attention_context = Variable(tr.zeros(batch_size, self.enc_dim))
    
    def decode_step(self, dec_in):
        """
        dec_in : (batch_size, 1, prenet_dim)
        self.attention_context: (batch_soze, 1, enc_dim)
        """
        # compute the lstm_att
        cat_lstm_att_in = tr.cat([dec_in, self.attention_context], dim=-1)
        self.lstm_att_h, self.lstm_att_c = self.lstm_att(cat_lstm_att_in, 
                                                         (self.lstm_att_h, self.lstm_att_c))
        
        # compute the attention_context and weights
        cat_attiont_weight = tr.cat([self.attention_weight, self.attention_weight_cum], dim=1)
        self.attention_context, self.attention_weight =  self.attention(self.lstm_att_h,
                                                                        self.value, cat_attiont_weight)
        self.attention_weight = self.attention_weight.unsqueeze(1)
        self.attention_weight_cum += self.attention_weight
        
        # compute the lstm_dec
        cat_lstm_dec_in = tr.cat([self.attention_context, self.lstm_att_h], dim=-1)
        self.lstm_dec_h, self.lstm_dec_c = self.lstm_dec(cat_lstm_dec_in, 
                                                         (self.lstm_dec_h, self.lstm_dec_c))
        
        # project the lstm_dec output to mel*time dimension
        proj_in = tr.cat([self.lstm_dec_h, self.attention_context], dim=-1)
        mel_out = self.mel_proj(proj_in)
        
        # compute the stop token
        stop_token = self.stop_proj(proj_in)
        return mel_out, stop_token
    
    def forward(self, value):
        self.init_parameters(value)
        processed_value = self.prenet(value)
        
        mel_outputs = []
        stop_tokens = []
        dec_idx = 0
        while len(mel_outputs) < value.shape[1]:
            mel_out, stop_token = self.decode_step(processed_value[:,dec_idx,:])
            mel_outputs.append(mel_out.unsqueeze(1))
            stop_tokens.append(stop_token.unsqueeze(1))
            dec_idx += 1
        mel_outputs = tr.cat(mel_outputs, dim=1)
        stop_tokens = tr.cat(stop_tokens, dim=1)
        return mel_outputs, stop_tokens
    
class Tacotron(tr.nn.Module):
    def __init__(self, n_mel=80, frames_per_step=2):
        super(Tacotron,self).__init__()
        self.encoder = TacoEncoder()
        self.decoder = TacoDecoder()
        self.postnet = post_net(n_mel * frames_per_step)
        
    def forward(self, tokens, tokens_len):
        enc_out = self.encoder(tokens, tokens_len)
        mel_outputs, stop_tokens = self.decoder(enc_out)
        # using the postnet to fit the residual of mel_outputs
        mel_outputs_res = mel_outputs + self.postnet(mel_outputs)
        return mel_outputs, mel_outputs_res, stop_tokens
    