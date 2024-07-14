'''
 * Copyright (c) 2021, salesforce.com, inc.
 * All rights reserved.
 * SPDX-License-Identifier: BSD-3-Clause
 * For full license text, see LICENSE.txt file in the repo root or https://opensource.org/licenses/BSD-3-Clause
'''

from functools import partial
from models.vit import VisionTransformer, interpolate_pos_embed
from models.xbert import BertConfig, BertForMaskedLM, BertModel
from models.transformer import TransformerEncoder
from models.tokenization_bert import BertTokenizer

import torch
import torch.nn.functional as F
from torch import nn

import numpy as np
import random


class EPMC(nn.Module):
    def __init__(self,                 
                 text_encoder = None,
                 tokenizer = None,
                 fusion_encoder = None,
                 config = None,    
                 temp = 0.07,
                 init_deit = True
                 ):
        super().__init__()

        self.tokenizer = tokenizer
        self.mlm_probability = config['mlm_probability']
        embed_dim = config['embed_dim']
        self.batch_size= config['batch_size']
        self.embed_dim= config['embed_dim']
     
        self.visual_encoder = VisionTransformer(
            img_size=config['image_res'], patch_size=16, embed_dim=768, depth=12, num_heads=12, 
            mlp_ratio=4, qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6)) 
        
        if init_deit:
            checkpoint = torch.hub.load_state_dict_from_url(
                url="https://dl.fbaipublicfiles.com/deit/deit_base_patch16_224-b5f2ef4d.pth", 
                map_location="cpu", check_hash=True) #检查点
            state_dict = checkpoint["model"]
            pos_embed_reshaped = interpolate_pos_embed(state_dict['pos_embed'], self.visual_encoder)
            state_dict['pos_embed'] = pos_embed_reshaped
            msg = self.visual_encoder.load_state_dict(state_dict,strict=False)
            print(msg)          
            
        vision_width = config['vision_width']       
        bert_config = BertConfig.from_json_file(config['bert_config'])
        # lou
        fusion_bert_config = BertConfig.from_json_file(config['fusion_bert_config'])
        
        self.text_encoder = BertModel.from_pretrained(text_encoder, config=bert_config, add_pooling_layer=False)  
        self.fusion_encoder = BertForMaskedLM.from_pretrained(fusion_encoder, config=fusion_bert_config) 

        text_width = self.text_encoder.config.hidden_size
        self.vision_proj = nn.Linear(vision_width, embed_dim)
        self.text_proj = nn.Linear(text_width, embed_dim)         

        self.temp = nn.Parameter(torch.ones([]) * config['temp'])   
        self.queue_size = config['queue_size']
        self.momentum = config['momentum']  
        self.itm_head = nn.Linear(embed_dim, 2)         

        # create the queue
        self.register_buffer("image_queue", torch.randn(embed_dim, self.queue_size))
        self.register_buffer("text_queue", torch.randn(embed_dim, self.queue_size))
        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))  
                             
        self.image_queue = nn.functional.normalize(self.image_queue, dim=0)
        self.text_queue = nn.functional.normalize(self.text_queue, dim=0)

        self.conv1d_t = nn.Conv1d(text_width, self.embed_dim, kernel_size=1, padding=0, bias=False)
        self.conv1d_i = nn.Conv1d(vision_width, self.embed_dim, kernel_size=1, padding=0, bias=False)

        self.conv1d_t_m = nn.Conv1d(text_width, self.embed_dim, kernel_size=1, padding=0, bias=False)
        self.conv1d_i_m = nn.Conv1d(vision_width, self.embed_dim, kernel_size=1, padding=0, bias=False)

        num_heads = 16
        attn_dropout = 0.1
        relu_dropout = 0.1
        res_dropout = 0.1
        embed_dropout = 0.1
        attn_mask = 'store_false'
        layers = 4
        
        self.trans_iichange = TransformerEncoder(embed_dim=embed_dim,
                                                 num_heads=num_heads,
                                                 layers=layers,
                                                 attn_dropout=attn_dropout,
                                                 relu_dropout=relu_dropout,
                                                 res_dropout=res_dropout,
                                                 embed_dropout=embed_dropout,
                                                 attn_mask=attn_mask)
        self.trans_ttchange = TransformerEncoder(embed_dim=embed_dim,
                                                 num_heads=num_heads,
                                                 layers=layers,
                                                 attn_dropout=attn_dropout,
                                                 relu_dropout=relu_dropout,
                                                 res_dropout=res_dropout,
                                                 embed_dropout=embed_dropout,
                                                 attn_mask=attn_mask)
        self.ii_proj = nn.Linear(embed_dim, embed_dim) 
        self.tt_proj = nn.Linear(embed_dim, embed_dim)    

        # create momentum models
        self.visual_encoder_m = VisionTransformer(
            img_size=config['image_res'], patch_size=16, embed_dim=768, depth=12, num_heads=12, 
            mlp_ratio=4, qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6)) 
        self.vision_proj_m = nn.Linear(vision_width, embed_dim)
        self.text_encoder_m = BertForMaskedLM.from_pretrained(text_encoder, config=bert_config)       
        self.text_proj_m = nn.Linear(text_width, embed_dim)    
        
        self.trans_iichange_m = TransformerEncoder(embed_dim=embed_dim,
                                                 num_heads=num_heads,
                                                 layers=layers,
                                                 attn_dropout=attn_dropout,
                                                 relu_dropout=relu_dropout,
                                                 res_dropout=res_dropout,
                                                 embed_dropout=embed_dropout,
                                                 attn_mask=attn_mask)
        self.trans_ttchange_m = TransformerEncoder(embed_dim=embed_dim,
                                                 num_heads=num_heads,
                                                 layers=layers,
                                                 attn_dropout=attn_dropout,
                                                 relu_dropout=relu_dropout,
                                                 res_dropout=res_dropout,
                                                 embed_dropout=embed_dropout,
                                                 attn_mask=attn_mask)
        self.ii_proj_m = nn.Linear(embed_dim, embed_dim) 
        self.tt_proj_m = nn.Linear(embed_dim, embed_dim)   


        self.trans_itchange = TransformerEncoder(embed_dim=embed_dim,
                                                 num_heads=num_heads,
                                                 layers=layers,
                                                 attn_dropout=attn_dropout,
                                                 relu_dropout=relu_dropout,
                                                 res_dropout=res_dropout,
                                                 embed_dropout=embed_dropout,
                                                 attn_mask=attn_mask)
        self.trans_tichange = TransformerEncoder(embed_dim=embed_dim,
                                                 num_heads=num_heads,
                                                 layers=layers,
                                                 attn_dropout=attn_dropout,
                                                 relu_dropout=relu_dropout,
                                                 res_dropout=res_dropout,
                                                 embed_dropout=embed_dropout,
                                                 attn_mask=attn_mask)
        self.it_proj = nn.Linear(embed_dim, embed_dim) 
        self.ti_proj = nn.Linear(embed_dim, embed_dim)    
        self.model_pairs = [[self.trans_iichange,self.trans_iichange_m],
                            [self.trans_ttchange,self.trans_ttchange_m],
                            [self.ii_proj,self.ii_proj_m],
                            [self.tt_proj,self.tt_proj_m],
                            [self.visual_encoder,self.visual_encoder_m],
                            [self.text_encoder,self.text_encoder_m],
                            [self.conv1d_t, self.conv1d_t_m],
                            [self.conv1d_i, self.conv1d_i_m]
                           ]

        self.copy_params()

    def forward(self, image, text, alpha=0):
        with torch.no_grad():
            self.temp.clamp_(0.001,0.5)
        
        image_embeds_old = self.visual_encoder(image) 
        image_atts = torch.ones(image_embeds_old.size()[:-1],dtype=torch.long).to(image.device) 

        text_output = self.text_encoder(text.input_ids, attention_mask = text.attention_mask, return_dict = True, mode = 'text')          
        text_embeds_old = text_output.last_hidden_state 

        # self-attention
        
        m_text_feat_tran = text_embeds_old.clone().transpose(1, 2)
        m_image_feat_tran = image_embeds_old.clone().transpose(1, 2)

        m_text_feat = self.conv1d_t(m_text_feat_tran).permute(2, 0, 1)
        m_image_feat = self.conv1d_i(m_image_feat_tran).permute(2, 0, 1)

        tt_embeds = self.trans_ttchange(m_text_feat, m_text_feat, m_text_feat).permute(1, 0, 2)
        ii_embeds = self.trans_iichange(m_image_feat, m_image_feat, m_image_feat).permute(1, 0, 2)
        text_feat = F.normalize(self.tt_proj(tt_embeds[:,0,:]),dim=-1)
        image_feat = F.normalize(self.ii_proj(ii_embeds[:,0,:]),dim=-1)

        i2t_embeds = self.trans_itchange(m_text_feat, m_image_feat, m_image_feat).permute(1, 0, 2)
        t2i_embeds = self.trans_tichange(m_image_feat, m_text_feat, m_text_feat).permute(1, 0, 2)
        i2t_feat = F.normalize(self.it_proj(i2t_embeds[:,0,:]),dim=-1)
        t2i_feat = F.normalize(self.ti_proj(t2i_embeds[:,0,:]),dim=-1)            

        text_embeds = torch.cat((tt_embeds,t2i_embeds), 1)
        image_embeds = torch.cat((ii_embeds,i2t_embeds), 1)
        
        with torch.no_grad():
            self._momentum_update()

            image_embeds_m_old = self.visual_encoder_m(image) 
            text_output_m = self.text_encoder_m.bert(text.input_ids, attention_mask = text.attention_mask,                      
                                                return_dict = True, mode = 'text') 
            text_embeds_m_old = text_output_m.last_hidden_state 

            m_text_feat_tran_m = text_embeds_m_old.clone().transpose(1, 2)
            m_image_feat_tran_m = image_embeds_m_old.clone().transpose(1, 2)

            m_text_feat_m = self.conv1d_t(m_text_feat_tran_m).permute(2, 0, 1)
            m_image_feat_m = self.conv1d_i(m_image_feat_tran_m).permute(2, 0, 1) 
            
            # cross-modal attention
            image_embeds_m = self.trans_iichange_m(m_image_feat_m, m_image_feat_m, m_image_feat_m).permute(1, 0, 2)
            text_embeds_m = self.trans_ttchange_m(m_text_feat_m, m_text_feat_m, m_text_feat_m).permute(1, 0, 2)
            image_feat_m = F.normalize(self.ii_proj_m(image_embeds_m[:,0,:]),dim=-1)
            text_feat_m = F.normalize(self.tt_proj_m(text_embeds_m[:,0,:]),dim=-1)
            image_feat_all = torch.cat([image_feat_m .t(),self.image_queue.clone().detach()],dim=1)                                               
            text_feat_all = torch.cat([text_feat_m.t(),self.text_queue.clone().detach()],dim=1)

            sim_i2t_m = image_feat_m @ text_feat_all / self.temp #p i2t 
            sim_t2i_m = text_feat_m @ image_feat_all / self.temp #p t2i  

            sim_targets = torch.zeros(sim_i2t_m.size()).to(image.device)
            sim_targets.fill_diagonal_(1)         

            sim_i2t_targets = alpha * F.softmax(sim_i2t_m, dim=1) + (1 - alpha) * sim_targets 
            sim_t2i_targets = alpha * F.softmax(sim_t2i_m, dim=1) + (1 - alpha) * sim_targets

                
        sim_i2t = image_feat @ text_feat_all / self.temp 
        sim_t2i = text_feat @ image_feat_all / self.temp      

        loss_i2t = -torch.sum(F.log_softmax(sim_i2t, dim=1)*sim_i2t_targets,dim=1).mean() 
        loss_t2i = -torch.sum(F.log_softmax(sim_t2i, dim=1)*sim_t2i_targets,dim=1).mean() 

        loss_ita = (loss_i2t+loss_t2i)/2 #L ita 

        sim_i2t_c = i2t_feat @ text_feat_all / self.temp #p c
        sim_t2i_c = t2i_feat @ image_feat_all / self.temp #p c 
        sim_targets_itc = torch.arange(0, self.batch_size).to(image.device)
        loss_i2t_c = F.cross_entropy(sim_i2t_c, sim_targets_itc)
        loss_t2i_c = F.cross_entropy(sim_t2i_c, sim_targets_itc)

        loss_itc = (loss_i2t_c+loss_t2i_c)/2 #L itc 

        self._dequeue_and_enqueue(image_feat_m, text_feat_m)

        text_atts = torch.ones(text_embeds.size()[:-1],dtype=torch.long).to(image.device)
        image_atts = torch.ones(image_embeds.size()[:-1],dtype=torch.long).to(image.device)
        output_pos = self.fusion_encoder.bert(encoder_embeds = text_embeds, 
                                       attention_mask = text_atts,
                                       encoder_hidden_states = image_embeds,
                                       encoder_attention_mask = image_atts,      
                                       return_dict = True,
                                       mode = 'fusion',
                                      )        

        with torch.no_grad():
            bs = image.size(0)        
            weights_i2t = F.softmax(sim_i2t_m[:,:bs],dim=1)
            weights_t2i = F.softmax(sim_t2i_m[:,:bs],dim=1)
            weights_i2t.fill_diagonal_(0) 
            weights_t2i.fill_diagonal_(0)

        image_embeds_neg = []    
        for b in range(bs):
            neg_idx = torch.multinomial(weights_t2i[b], 1).item() 
            image_embeds_neg.append(image_embeds[neg_idx])
        image_embeds_neg = torch.stack(image_embeds_neg,dim=0)   

        text_embeds_neg = []
        text_atts_neg = []
        for b in range(bs):
            neg_idx = torch.multinomial(weights_i2t[b], 1).item()
            text_embeds_neg.append(text_embeds[neg_idx])
            text_atts_neg.append(text_atts[neg_idx])
        text_embeds_neg = torch.stack(text_embeds_neg,dim=0)   
        text_atts_neg = torch.stack(text_atts_neg,dim=0)      

        text_embeds_all = torch.cat([text_embeds, text_embeds_neg],dim=0)     
        text_atts_all = torch.cat([text_atts, text_atts_neg],dim=0)     

        image_embeds_all = torch.cat([image_embeds_neg,image_embeds],dim=0)
        image_atts_all = torch.cat([image_atts,image_atts],dim=0)

        output_neg = self.fusion_encoder.bert(encoder_embeds = text_embeds_all, 
                                        attention_mask = text_atts_all,
                                        encoder_hidden_states = image_embeds_all,
                                        encoder_attention_mask = image_atts_all,      
                                        return_dict = True,
                                        mode = 'fusion',
                                       )                      

        vl_embeddings = torch.cat([output_pos.last_hidden_state[:,0,:], output_neg.last_hidden_state[:,0,:]],dim=0)
        vl_output = self.itm_head(vl_embeddings)            

        itm_labels = torch.cat([torch.ones(bs,dtype=torch.long),torch.zeros(2*bs,dtype=torch.long)],
                               dim=0).to(image.device)
        loss_itm = F.cross_entropy(vl_output, itm_labels)   

        #MLM                
        input_ids = text.input_ids.clone()
        labels = input_ids.clone()

        probability_matrix = torch.full(labels.shape, self.mlm_probability)                    
        input_ids, labels = self.mask(input_ids, self.fusion_encoder.config.vocab_size, image.device, targets=labels,
                                      probability_matrix = probability_matrix) 
        
        with torch.no_grad():
            image_atts_m = torch.ones(image_embeds_m.size()[:-1],dtype=torch.long).to(image.device)
            logits_m = self.fusion_encoder(input_ids, 
                                           attention_mask = text.attention_mask,
                                           encoder_hidden_states = image_embeds_m,
                                           encoder_attention_mask = image_atts_m,      
                                           return_dict = True,
                                           return_logits = True,   
                                          )    
        logits_m = torch.clamp(logits_m, min=1e-7, max=1 - 1e-7)
        mlm_output = self.fusion_encoder(input_ids, 
                                       attention_mask = text.attention_mask,
                                       encoder_hidden_states = image_embeds,
                                       encoder_attention_mask = image_atts,      
                                       return_dict = True,
                                       labels = labels,   
                                       soft_labels = F.softmax(logits_m,dim=-1),
                                       alpha = alpha
                                      )                           
        loss_mlm = mlm_output.loss

        ita_t = 1
        loss_ita = loss_ita * ita_t
        itc_t = 1
        loss_itc = loss_itc * itc_t          

        te = torch.cat((text_feat,t2i_feat), 1)
        ie = torch.cat((image_feat,i2t_feat), 1)

        return loss_mlm, loss_ita, loss_itc, loss_itm,te,ie 

        

    @torch.no_grad()    
    def copy_params(self):
        for model_pair in self.model_pairs:           
            for param, param_m in zip(model_pair[0].parameters(), model_pair[1].parameters()):
                param_m.data.copy_(param.data)  # initialize
                param_m.requires_grad = False  # not update by gradient    

            
    @torch.no_grad()        
    def _momentum_update(self):
        for model_pair in self.model_pairs:           
            for param, param_m in zip(model_pair[0].parameters(), model_pair[1].parameters()):
                param_m.data = param_m.data * self.momentum + param.data * (1. - self.momentum) 
                
            
            
    @torch.no_grad()
    def _dequeue_and_enqueue(self, image_feat, text_feat):
        # gather keys before updating queue
        image_feats = concat_all_gather(image_feat)
        text_feats = concat_all_gather(text_feat)

        batch_size = image_feats.shape[0]

        ptr = int(self.queue_ptr)
        assert self.queue_size % batch_size == 0  # for simplicity

        # replace the keys at ptr (dequeue and enqueue)
        self.image_queue[:, ptr:ptr + batch_size] = image_feats.T
        self.text_queue[:, ptr:ptr + batch_size] = text_feats.T
        ptr = (ptr + batch_size) % self.queue_size  # move pointer

        self.queue_ptr[0] = ptr 
        
        
    def mask(self, input_ids, vocab_size, device, targets=None, masked_indices=None, probability_matrix=None):
        if masked_indices is None:                                       
            masked_indices = torch.bernoulli(probability_matrix).bool()
                                               
        masked_indices[input_ids == self.tokenizer.pad_token_id] = False
        masked_indices[input_ids == self.tokenizer.cls_token_id] = False
        
        if targets is not None:
            targets[~masked_indices] = -100 # We only compute loss on masked tokens            

        # 80% of the time, we replace masked input tokens with tokenizer.mask_token ([MASK])
        indices_replaced = torch.bernoulli(torch.full(input_ids.shape, 0.8)).bool() & masked_indices
        input_ids[indices_replaced] = self.tokenizer.mask_token_id

        # 10% of the time, we replace masked input tokens with random word
        indices_random = torch.bernoulli(torch.full(input_ids.shape, 0.5)).bool() & masked_indices & ~indices_replaced
        random_words = torch.randint(vocab_size, input_ids.shape, dtype=torch.long).to(device)
        input_ids[indices_random] = random_words[indices_random]                     
        # The rest of the time (10% of the time) we keep the masked input tokens unchanged   
        
        if targets is not None:
            return input_ids, targets
        else:
            return input_ids
        

@torch.no_grad()
def concat_all_gather(tensor):
    """
    Performs all_gather operation on the provided tensors.
    *** Warning ***: torch.distributed.all_gather has no gradient.
    """
    tensors_gather = [torch.ones_like(tensor)
        for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

    output = torch.cat(tensors_gather, dim=0)
    return output

