import torch
import torch.nn as nn
from torch.nn.utils.rnn import  pack_padded_sequence, pad_packed_sequence
from transformers import BertModel, BertConfig
from einops.layers.torch import Rearrange

# let's define a simple model that can deal with multimodal variable length sequence
class MISA(nn.Module):
    def __init__(self, config):
        super(MISA, self).__init__()
        self.config = config
        self.text_size = config.embedding_size
        self.visual_size = config.visual_size
        self.acoustic_size = config.acoustic_size
        self.input_sizes = input_sizes = [self.text_size, self.visual_size, self.acoustic_size]
        self.hidden_sizes = hidden_sizes = [int(self.text_size), int(self.visual_size), int(self.acoustic_size)]
        self.output_size = output_size = config.num_classes
        self.dropout_rate = dropout_rate = config.dropout
        self.activation = self.config.activation()
        self.tanh = nn.Tanh()

        rnn = nn.LSTM if self.config.rnncell == "lstm" else nn.GRU
        # defining modules - two layer bidirectional LSTM with layer norm in between

        # Initializing a BERT bert-base-uncased style configuration
        bertconfig = BertConfig.from_pretrained('bert-base-uncased', output_hidden_states=True)
        self.bertmodel = BertModel.from_pretrained('bert-base-uncased', config=bertconfig)

        self.vrnn1 = rnn(input_sizes[1], hidden_sizes[1], bidirectional=True)
        self.vrnn2 = rnn(2 * hidden_sizes[1], hidden_sizes[1], bidirectional=True)

        self.arnn1 = rnn(input_sizes[2], hidden_sizes[2], bidirectional=True)
        self.arnn2 = rnn(2 * hidden_sizes[2], hidden_sizes[2], bidirectional=True)

        ##########################################
        # mapping modalities to same sized space
        ##########################################

        self.project_t = nn.Sequential()
        self.project_t.add_module('project_t', nn.Linear(in_features=768, out_features=config.hidden_size))
        self.project_t.add_module('project_t_activation', self.activation)
        self.project_t.add_module('project_t_layer_norm', nn.LayerNorm(config.hidden_size))


        self.project_v = nn.Sequential()
        self.project_v.add_module('project_v',
                                  nn.Linear(in_features=hidden_sizes[1] * 4, out_features=config.hidden_size))
        self.project_v.add_module('project_v_activation', self.activation)
        self.project_v.add_module('project_v_layer_norm', nn.LayerNorm(config.hidden_size))

        self.project_a = nn.Sequential()
        self.project_a.add_module('project_a',
                                  nn.Linear(in_features=hidden_sizes[2] * 4, out_features=config.hidden_size))
        self.project_a.add_module('project_a_activation', self.activation)
        self.project_a.add_module('project_a_layer_norm', nn.LayerNorm(config.hidden_size))


        ##########################################
        # shared encoder
        ##########################################
        self.shared1 = nn.Sequential()
        self.shared1.add_module('shared_1', nn.Linear(in_features=config.hidden_size, out_features=config.hidden_size))
        self.shared1.add_module('shared_activation_1', nn.Sigmoid())

        self.shared2 = nn.Sequential()
        self.shared2.add_module('shared_2', nn.Linear(in_features=config.hidden_size, out_features=config.hidden_size))
        self.shared2.add_module('shared_activation_2', nn.Sigmoid())

        self.fusion = nn.Sequential()
        self.fusion.add_module('fusion_layer_1', nn.Linear(in_features=self.config.hidden_size * 2,
                                                           out_features=6 * self.config.hidden_size, bias = False))
        self.fusion.add_module('fusion_layer_1_dropout', nn.Dropout(dropout_rate))
        self.fusion.add_module('fusion_layer_1_activation', self.activation)
        self.fusion.add_module('fusion_layer_3',
                               nn.Linear(in_features=6 * self.config.hidden_size, out_features=output_size, bias = False))

        self.MLP_Communicator1 = MLP_Communicator(self.config.hidden_size, 2, hidden_size=64, depth=1)
        self.MLP_Communicator2 = MLP_Communicator(self.config.hidden_size, 2, hidden_size=64, depth=1)

        self.batchnorm = nn.BatchNorm1d(2, affine=False)

        # encoder_layer1 = nn.TransformerEncoderLayer(d_model=self.config.hidden_size, nhead=2)
        # self.transformer_encoder1 = nn.TransformerEncoder(encoder_layer1, num_layers=1)
        # encoder_layer2 = nn.TransformerEncoderLayer(d_model=self.config.hidden_size, nhead=2)
        # self.transformer_encoder2 = nn.TransformerEncoder(encoder_layer2, num_layers=1)

    def extract_features(self, sequence, lengths, rnn1, rnn2, layer_norm):
        packed_sequence = pack_padded_sequence(sequence, lengths)

        if self.config.rnncell == "lstm":
            packed_h1, (final_h1, _) = rnn1(packed_sequence)
        else:
            packed_h1, final_h1 = rnn1(packed_sequence)

        padded_h1, _ = pad_packed_sequence(packed_h1)
        normed_h1 = layer_norm(padded_h1)
        packed_normed_h1 = pack_padded_sequence(normed_h1, lengths)

        if self.config.rnncell == "lstm":
            _, (final_h2, _) = rnn2(packed_normed_h1)
        else:
            _, final_h2 = rnn2(packed_normed_h1)

        return final_h1, final_h2

    def alignment(self, visual, acoustic, lengths, bert_sent, bert_sent_type, bert_sent_mask):

        batch_size = lengths.shape[0]

        bert_output = self.bertmodel(input_ids=bert_sent,
                                     attention_mask=bert_sent_mask,
                                     token_type_ids=bert_sent_type)

        bert_output = bert_output[0]

        # masked mean
        masked_output = torch.mul(bert_sent_mask.unsqueeze(2), bert_output)
        mask_len = torch.sum(bert_sent_mask, dim=1, keepdim=True)
        bert_output = torch.sum(masked_output, dim=1, keepdim=False) / mask_len

        utterance_text = bert_output

        # extract features from visual modality
        final_h1v, final_h2v = self.extract_features(visual, lengths, self.vrnn1, self.vrnn2, self.vlayer_norm)
        utterance_video = torch.cat((final_h1v, final_h2v), dim=2).permute(1, 0, 2).contiguous().view(batch_size, -1)

        # extract features from acoustic modality
        final_h1a, final_h2a = self.extract_features(acoustic, lengths, self.arnn1, self.arnn2, self.alayer_norm)
        utterance_audio = torch.cat((final_h1a, final_h2a), dim=2).permute(1, 0, 2).contiguous().view(batch_size, -1)

        # Shared-private encoders
        self.shared_modaties(utterance_text, utterance_video, utterance_audio)

        h1 = torch.stack((self.utt_shared_v, self.utt_shared_t), dim=0)
        h2 = torch.stack((self.utt_shared_a2, self.utt_shared_t2), dim=0)

        h1 = self.batchnorm(h1.permute(1, 0, 2))
        h2 = self.batchnorm(h2.permute(1, 0, 2))

        # h1 = self.transformer_encoder1(h1).permute(1, 0, 2)
        # h2 = self.transformer_encoder2(h2).permute(1, 0, 2)

        h1 = self.MLP_Communicator1(h1).permute(1,0,2)
        h2 = self.MLP_Communicator2(h2).permute(1,0,2)

        # h1 = h1.permute(2, 0, 1)
        # h2 = h2.permute(2, 0, 1)
        h1 = torch.cat((h1[0], h1[1]), dim=1)
        h2 = torch.cat((h2[0], h2[1]), dim=1)

        norm1 = torch.norm(h1, dim = 1,p=1)
        norm2 = torch.norm(h2, dim = 1,p=1)

        self.scale = norm2
        self.polar_vector = h1

        h1 = h1 * (torch.div(norm2.unsqueeze(1), norm1.unsqueeze(1)))

        o7 = self.fusion(h1)
        return o7

    def shared_modaties(self, utterance_t, utterance_v, utterance_a):

        # Projecting to same sized space
        self.utt_t_orig = utterance_t = self.project_t(utterance_t)
        self.utt_v_orig = utterance_v = self.project_v(utterance_v)
        self.utt_a_orig = utterance_a = self.project_a(utterance_a)

        self.utt_shared_t = self.shared1(utterance_t)
        self.utt_shared_v = self.shared1(utterance_v)

        self.utt_shared_t2 = self.shared2(utterance_t)
        self.utt_shared_a2 = self.shared2(utterance_a)

    def forward(self, video, acoustic, lengths, bert_sent, bert_sent_type, bert_sent_mask):
        o = self.alignment(video, acoustic, lengths, bert_sent, bert_sent_type, bert_sent_mask)
        return o

class MLP_block(nn.Module):
    def __init__(self, input_size, hidden_size, dropout=0.5):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size, input_size),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        x = self.net(x)
        return x


class MLP_Communicator(nn.Module):
    def __init__(self, token, channel, hidden_size, depth=1):
        super(MLP_Communicator, self).__init__()
        self.depth = depth
        self.token_mixer = nn.Sequential(
            Rearrange('b n d -> b d n'),
            MLP_block(input_size=channel, hidden_size=hidden_size),
            Rearrange('b n d -> b d n')
        )
        self.channel_mixer = nn.Sequential(
            MLP_block(input_size=token, hidden_size=hidden_size)
        )

    def forward(self, x):
        for _ in range(self.depth):
            x = x + self.token_mixer(x)
            x = x + self.channel_mixer(x)
        return x
