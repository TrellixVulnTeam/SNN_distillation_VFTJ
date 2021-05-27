#---------------------------------------------------
# Imports
#---------------------------------------------------

#python snn.py --dataset CIFAR10 --epoch 300 --batch_size 64 --architecture VGG16 --learning_rate 1e-4 --epochs 10 --lr_interval '0.60 0.80 0.90' --lr_reduce 5 --timesteps 10 --leak 1.0 --scaling_factor 0.6 --optimizer Adam --weight_decay 0 --momentum 0 --amsgrad True --dropout 0.1 --train_acc_batches 50 --default_threshold 1.0 --pretrained_ann trained_models/ann_vgg16_cifar10_best_model1.pth
#python snn.py --dataset CIFAR100 --epoch 300 --batch_size 64 --architecture VGG16 --learning_rate 5e-4 --epochs 300 --lr_interval '0.60 0.80 0.90' --lr_reduce 10 --timesteps 5 --leak 1.0 --scaling_factor 0.6 --optimizer Adam --weight_decay 0 --momentum 0 --amsgrad True --dropout 0.1 --default_threshold 1.0 --pretrained_ann trained_models/ann_vgg16_cifar100_best_model.pth
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import pdb
import math
from collections import OrderedDict
from matplotlib import pyplot as plt
import copy

cfg = {
	'VGG4' : [64, 'A', 128, 'A'],
    'VGG5' : [64, 'A', 128, 128, 'A'],
    'VGG9':  [64, 'A', 128, 256, 'A', 256, 512, 'A', 512, 'A', 512],
    'VGG11': [64, 'A', 128, 256, 'A', 512, 512, 'A', 512, 'A', 512, 512],
    'VGG13': [64, 64, 'A', 128, 128, 'A', 256, 256, 'A', 512, 512, 512, 'A', 512],
    'VGG16': [64, 64, 'A', 128, 128, 'A', 256, 256, 256, 'A', 512, 512, 512, 'A', 512, 512, 512],
    'VGG19': [64, 64, 'A', 128, 128, 'A', 256, 256, 256, 256, 'A', 512, 512, 512, 512, 'A', 512, 512, 512, 512],
    'CIFARNET': [128, 256, 'A', 512, 'A', 1204, 512]
}

class LinearSpike(torch.autograd.Function):
    """
    Here we use the piecewise-linear surrogate gradient as was done
    in Bellec et al. (2018).
    """
    gamma = 0.3 # Controls the dampening of the piecewise-linear surrogate gradient

    @staticmethod
    def forward(ctx, input):
        
        ctx.save_for_backward(input)
        out = torch.zeros_like(input).cuda()
        out[input > 0] = 1.0
        return out

    @staticmethod
    def backward(ctx, grad_output):
        
        input,     = ctx.saved_tensors
        grad_input = grad_output.clone()
        grad       = LinearSpike.gamma*F.threshold(1.0-torch.abs(input), 0, 0)
        return grad*grad_input, None

class VGG_SNN_STDB(nn.Module):

	def __init__(self, vgg_name, activation='Linear', labels=10, timesteps=100, leak=1.0, default_threshold = 1.0, dropout=0.2, kernel_size=3, dataset='CIFAR10', individual_thresh=False, vmem_drop=0,input_compress_num=0,rank_reduce=False,cal_neuron=False):
		super().__init__()
		
		self.vgg_name 		= vgg_name
		if activation == 'Linear':
			self.act_func 	= LinearSpike.apply
		elif activation == 'STDB':
			self.act_func	= STDB.apply
		self.labels 		= labels
		self.timesteps 		= timesteps
		self.cal_neuron     = cal_neuron
		#STDB.alpha 		 	= alpha
		#STDB.beta 			= beta 
		self.dropout 		= dropout
		self.kernel_size 	= kernel_size
		self.dataset 		= dataset
		self.individual_thresh = individual_thresh
		self.vmem_drop 		= vmem_drop
		#self.threshold 		= nn.ParameterDict()
		#self.leak 			= nn.ParameterDict()
		self.mem 			= {}
		self.mem_thr_tmp_conv 	= {}
		self.all_neuron_num_conv = {}		
		self.mem_thr_tmp_linear 	= {}
		self.all_neuron_num_linear = {}
		self.mask 			= {}
		self.spike 			= {}
		self.input_compress_num = input_compress_num
		self.rank_reduce   = rank_reduce
		self.features, self.classifier = self._make_layers(cfg[self.vgg_name])
		
		self._initialize_weights2()

		threshold 	= {}
		lk 	  		= {}
		if self.dataset in ['CIFAR10', 'CIFAR100']:
			width = 32
			height = 32
		elif self.dataset=='MNIST':
			width = 28
			height = 28
		elif self.dataset=='IMAGENET':
			width = 224
			height = 224

		for l in range(len(self.features)):
			if isinstance(self.features[l], nn.Conv2d):
				if self.individual_thresh:
					threshold['t'+str(l)] 	= nn.Parameter(torch.ones(self.features[l].out_channels, width, height)*default_threshold)
					lk['l'+str(l)] 			= nn.Parameter(torch.ones(self.features[l].out_channels, width, height)*leak)
				else:
					threshold['t'+str(l)] 	= nn.Parameter(torch.tensor(default_threshold))
					lk['l'+str(l)]			= nn.Parameter(torch.tensor(leak))
				#threshold['t'+str(l)] 	= nn.Parameter(torch.empty(1,1).fill_(default_threshold))
				#lk['l'+str(l)]			= nn.Parameter(torch.empty(1,1).fill_(leak))
			elif isinstance(self.features[l], nn.AvgPool2d):
				width 	= width//self.features[l].kernel_size
				height 	= height//self.features[l].kernel_size
				
				
		prev = len(self.features)
		for l in range(len(self.classifier)-1):
			if isinstance(self.classifier[l], nn.Linear):
				if self.individual_thresh:
					threshold['t'+str(prev+l)]	= nn.Parameter(torch.ones(self.classifier[l].out_features)*default_threshold)
					lk['l'+str(prev+l)] 		= nn.Parameter(torch.ones(self.classifier[l].out_features)*leak)
				else:
					threshold['t'+str(prev+l)] 	= nn.Parameter(torch.tensor(default_threshold))
					lk['l'+str(prev+l)] 		= nn.Parameter(torch.tensor(leak))
				#threshold['t'+str(prev+l)] 	= nn.Parameter(torch.empty(1,1).fill_(default_threshold))
				#lk['l'+str(prev+l)] 		= nn.Parameter(torch.empty(1,1).fill_(leak))

		#pdb.set_trace()
		self.threshold 	= nn.ParameterDict(threshold)
		self.leak 		= nn.ParameterDict(lk)
		
	def _initialize_weights2(self):
		for m in self.modules():
            
			if isinstance(m, nn.Conv2d):
				n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
				m.weight.data.normal_(0, math.sqrt(2. / n))
				if m.bias is not None:
					m.bias.data.zero_()
			elif isinstance(m, nn.BatchNorm2d):
				m.weight.data.fill_(1)
				m.bias.data.zero_()
			elif isinstance(m, nn.Linear):
				n = m.weight.size(1)
				m.weight.data.normal_(0, 0.01)
				if m.bias is not None:
					m.bias.data.zero_()

	def threshold_update(self, scaling_factor=1.0, thresholds=[]):

		# Initialize thresholds
		self.scaling_factor = scaling_factor
		
		if self.dataset in ['CIFAR10', 'CIFAR100']:
			width = 32
			height = 32
		elif self.dataset=='MNIST':
			width = 28
			height = 28
		elif self.dataset=='IMAGENET':
			width = 224
			height = 224

		for pos in range(len(self.features)):
			if isinstance(self.features[pos], nn.Conv2d):
				if thresholds:
					if self.individual_thresh:
						self.threshold.update({'t'+str(pos): nn.Parameter(torch.ones(self.features[pos].out_channels, width, height)*thresholds.pop(0)*self.scaling_factor)})
					else:
						self.threshold.update({'t'+str(pos): nn.Parameter(torch.tensor(thresholds.pop(0))*self.scaling_factor)})
				#print('\t Layer{} : {:.2f}'.format(pos, self.threshold[pos]))
			elif isinstance(self.features[pos], nn.AvgPool2d):
				width 	= width//self.features[pos].kernel_size
				height 	= height//self.features[pos].kernel_size

		prev = len(self.features)

		for pos in range(len(self.classifier)-1):
			if isinstance(self.classifier[pos], nn.Linear):
				if thresholds:
					if self.individual_thresh:
						self.threshold.update({'t'+str(prev+pos): nn.Parameter(torch.ones(self.classifier[pos].out_features)*thresholds.pop(0)*self.scaling_factor)})
					else:
						self.threshold.update({'t'+str(prev+pos): nn.Parameter(torch.tensor(thresholds.pop(0))*self.scaling_factor)})
				#print('\t Layer{} : {:.2f}'.format(prev+pos, self.threshold[prev+pos]))


	def _make_layers(self, cfg):
		layers 		= []
		if self.dataset =='MNIST':
			in_channels = 1
		else:
			in_channels = 3

		for i,x in enumerate(cfg):
			stride = 1
						
			if x == 'A':
				layers.pop()
				layers += [nn.AvgPool2d(kernel_size=2, stride=2)]
			
			else:	
				if (self.input_compress_num != 0) and (i==0):
					layers += [nn.Conv2d(in_channels, int(x-self.input_compress_num), kernel_size=self.kernel_size, padding=(self.kernel_size-1)//2, stride=stride, bias=False),
								nn.ReLU(inplace=True)
								]	
					in_channels = int(x-self.input_compress_num)		
							
    			
				else:
					layers += [nn.Conv2d(in_channels, x, kernel_size=self.kernel_size, padding=(self.kernel_size-1)//2, stride=stride, bias=False),
									nn.ReLU(inplace=True)
								]
					in_channels = x    				
					
					
				layers += [nn.Dropout(self.dropout)]
    				
    				

		if self.dataset== 'IMAGENET':
			layers.pop()
			layers += [nn.AvgPool2d(kernel_size=2, stride=2)]
			
		features = nn.Sequential(*layers)
		
		layers = []
		if self.dataset == 'IMAGENET':
			layers += [nn.Linear(512*7*7, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, self.labels, bias=False)]

		elif self.vgg_name == 'VGG5' and self.dataset != 'MNIST':
			layers += [nn.Linear(512*4*4, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, self.labels, bias=False)]

		elif self.vgg_name == 'VGG4' and self.dataset== 'MNIST':
			layers += [nn.Linear(128*7*7, 1024, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			#layers += [nn.Linear(4096, 4096, bias=False)]
			#layers += [nn.ReLU(inplace=True)]
			#layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(1024, self.labels, bias=False)]
		
		elif self.vgg_name != 'VGG5' and self.dataset != 'MNIST':
			layers += [nn.Linear(512*2*2, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, self.labels, bias=False)]
		
		elif self.vgg_name == 'VGG5' and self.dataset == 'MNIST':
			layers += [nn.Linear(128*7*7, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, self.labels, bias=False)]

		elif self.vgg_name != 'VGG5' and self.dataset == 'MNIST':
			layers += [nn.Linear(512*1*1, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, 4096, bias=False)]
			layers += [nn.ReLU(inplace=True)]
			layers += [nn.Dropout(self.dropout)]
			layers += [nn.Linear(4096, self.labels, bias=False)]


		classifer = nn.Sequential(*layers)
		return (features, classifer)

	def network_update(self, timesteps):
		self.timesteps 	= timesteps
		#for key, value in sorted(self.leak.items(), key=lambda x: (int(x[0][1:]), (x[1]))):
		#	if isinstance(leak, list) and leak:
		#		self.leak.update({key: nn.Parameter(torch.tensor(leak.pop(0)))})
	
	def neuron_init(self, x):
		self.batch_size = x.size(0)
		self.width 		= x.size(2)
		self.height 	= x.size(3)			
		
		self.mem 	= {}
		self.mem_thra 	= {}
		self.spike 	= {}
		self.mask 	= {}

		for l in range(len(self.features)):
								
			if isinstance(self.features[l], nn.Conv2d):
    
				self.mem[l] 		= torch.zeros(self.batch_size, self.features[l].out_channels, self.width, self.height).cuda()
				if(l == 0):
					self.input_rank = torch.zeros(self.timesteps,self.batch_size, self.features[l].out_channels, self.width, self.height).cuda()   
			# elif isinstance(self.features[l], nn.ReLU):
			# 	if isinstance(self.features[l-1], nn.Conv2d):
			# 		self.spike[l] 	= torch.ones(self.mem[l-1].shape,requires_grad = False)*(-1000)
			# 	elif isinstance(self.features[l-1], nn.AvgPool2d):
			# 		self.spike[l] 	= torch.ones(self.batch_size, self.features[l-2].out_channels, self.width, self.height,requires_grad = False)*(-1000)

			elif isinstance(self.features[l], nn.Dropout):
				self.mask[l] = self.features[l](torch.ones(self.mem[l-2].shape).cuda())

			elif isinstance(self.features[l], nn.AvgPool2d):
				self.width = self.width//self.features[l].kernel_size
				self.height = self.height//self.features[l].kernel_size
		
		prev = len(self.features)

		for l in range(len(self.classifier)):
			
			if isinstance(self.classifier[l], nn.Linear):
				self.mem[prev+l] 		= torch.zeros(self.batch_size, self.classifier[l].out_features).cuda()
			
			# elif isinstance(self.classifier[l], nn.ReLU):
			# 	self.spike[prev+l] 		= torch.ones(self.mem[prev+l-1].shape,requires_grad = False)*(-1000)

			elif isinstance(self.classifier[l], nn.Dropout):
				self.mask[prev+l] = self.classifier[l](torch.ones(self.mem[prev+l-2].shape).cuda())
				
		# self.spike = copy.deepcopy(self.mem)
		# for key, values in self.spike.items():
		# 	for value in values:
		# 		value.fill_(-1000)

	def percentile(self, t, q):

		k = 1 + round(.01 * float(q) * (t.numel() - 1))
		result = t.view(-1).kthvalue(k).values.item()
		return result
	
	def custom_dropout(self, tensor, prob, conv=True):
		if prob==0:
			return tensor
		mask_retain			= F.dropout(tensor, p=prob, training=True).bool()*1
		mask_drop 			= (mask_retain==0)*1
		tensor_retain 		= tensor*mask_retain
		tensor_drop 		= tensor*mask_drop
		if conv:
			tensor_drop_sum	= tensor_drop.sum(dim=(2,3))
			retain_no 		= mask_retain.sum(dim=(2,3))
			tensor_drop_sum[retain_no==0] = 0
			retain_no[retain_no==0] = 1
			increment 		= tensor_drop_sum/(retain_no)
			increment_array = increment.repeat_interleave(tensor.shape[2]*tensor.shape[3]).view(tensor.shape)
		else:
			#pdb.set_trace()
			tensor_drop_sum = tensor_drop.sum(dim=1)
			retain_no 		= mask_retain.sum(dim=1)
			tensor_drop_sum[retain_no==0] = 0
			retain_no[retain_no==0] = 1
			increment 		= tensor_drop_sum/(retain_no)	
			increment_array = increment.repeat_interleave(tensor.shape[1]).view(tensor.shape)
			
		increment_array = increment_array*mask_retain
		new_tensor 		= tensor_retain+increment_array
		
		return new_tensor

	def forward(self, x, find_max_mem=False, is_feat=False,max_mem_layer=0, percentile=99.7):
		
		self.neuron_init(x)
		max_mem=0.0
		self.mem_thr_tmp_conv 	= {}
		self.all_neuron_num_conv = {}		
		self.mem_thr_tmp_linear 	= {}
		self.all_neuron_num_linear = {}
		# midle_features = {}
		# if find_max_mem:
		# 	prob=self.vmem_drop
		# else:
		# 	prob=self.vmem_drop
		#ann = [0]*len(self.features)
		#ann[1] = 1
		#pdb.set_trace()
		for t in range(self.timesteps):
			out_prev = x
			# keys = [*self.mem]
			# print('time: {}'.format(t), end=', ')
			# for l, key in enumerate(keys):
			# 	print('l{}: {:.1f}'.format(l+1, self.mem[key].max()), end=', ')
			# print()
			# input()
			for l in range(len(self.features)):
				if isinstance(self.features[l], (nn.Conv2d)):
					
					if find_max_mem and l==max_mem_layer:
						cur = self.percentile(self.features[l](out_prev).view(-1), percentile)
						if (cur>max_mem):
							max_mem = torch.tensor([cur])
						break
					delta_mem 		= self.features[l](out_prev)
					# print(delta_mem)
					self.mem[l] 	= getattr(self.leak, 'l'+str(l)) *self.mem[l] + delta_mem
					mem_thr 		= (self.mem[l]/getattr(self.threshold, 't'+str(l))) - 1.0
					rst 			= getattr(self.threshold, 't'+str(l)) * (mem_thr>0).float()
					self.mem[l] 	= self.mem[l]-rst
					#out_prev 		= self.features[l](out_prev)

					
				elif isinstance(self.features[l], nn.ReLU):
					#pdb.set_trace()
					out 			= self.act_func(mem_thr)

					if(self.cal_neuron):
						if(t==0):
							self.all_neuron_num_conv[l] =  out.size()[0]*out.size()[1]*out.size()[2]*out.size()[3]
							self.mem_thr_tmp_conv[l] = out.sum().to('cpu').detach().numpy().copy()

							
						else:
							self.mem_thr_tmp_conv[l] += out.sum().to('cpu').detach().numpy().copy()





					# self.spike[l] 	= self.spike[l].masked_fill(out.bool(),t-1)
					out_prev  		= out.clone()
					# print(out_prev.sum())
					# print(out_prev.size()[0]*out_prev.size()[1]*out_prev.size()[2]*out_prev.size()[3])
					# print(out_prev.size())

				elif isinstance(self.features[l], nn.AvgPool2d):
					out_prev 		= self.features[l](out_prev)
				
				elif isinstance(self.features[l], nn.Dropout):
					out_prev 		= out_prev * self.mask[l]
			
			if find_max_mem and max_mem_layer<len(self.features):
				continue

			out_prev       	= out_prev.reshape(self.batch_size, -1)
			prev = len(self.features)
			#pdb.set_trace()
			for l in range(len(self.classifier)-1):
													
				if isinstance(self.classifier[l], (nn.Linear)):
					
					if find_max_mem and (prev+l)==max_mem_layer:
						#pdb.set_trace()
						cur = self.percentile(self.classifier[l](out_prev).view(-1), percentile)
						if cur>max_mem:
							max_mem = torch.tensor([cur])
						break
					delta_mem 			= self.classifier[l](out_prev)
					self.mem[prev+l] 	= getattr(self.leak, 'l'+str(prev+l)) * self.mem[prev+l] + delta_mem
					mem_thr 			= (self.mem[prev+l]/getattr(self.threshold, 't'+str(prev+l))) - 1.0
	
					rst 				= getattr(self.threshold,'t'+str(prev+l)) * (mem_thr>0).float()
					self.mem[prev+l] 	= self.mem[prev+l]-rst

				
				elif isinstance(self.classifier[l], nn.ReLU):
					out 				= self.act_func(mem_thr)

					if(self.cal_neuron):
						if(t==0):
							self.all_neuron_num_linear[l] =  out.size()[0]*out.size()[1]
							self.mem_thr_tmp_linear[l] = out.sum().to('cpu').detach().numpy().copy()
							
						else:
							self.mem_thr_tmp_linear[l] += out.sum().to('cpu').detach().numpy().copy()
							
					# self.spike[prev+l] 	= self.spike[prev+l].masked_fill(out.bool(),t-1)
					out_prev  			= out.clone()
			

				elif isinstance(self.classifier[l], nn.Dropout):
					out_prev 		= out_prev * self.mask[prev+l]
			
			# Compute the classification layer outputs
			if not find_max_mem:
				self.mem[prev+l+1] 		= self.mem[prev+l+1] + self.classifier[l+1](out_prev)

		if find_max_mem:
			return max_mem
		
		if self.rank_reduce:
			a = self.input_rank.shape[0]
			b = self.input_rank.shape[1]
			
			for t in range(self.timesteps):
				c = torch.tensor([torch.matrix_rank(self.input_rank[t,i,j,:,:]/self.timesteps).cuda().item() for i in range(a) for j in range(b)]).cuda()
				c = c.view(a, -1).float()
				if t == 0:
					c_sum = c.sum(0)
				else:
					c_sum += c.sum(0)
    				

			return self.mem[prev+l+1],c_sum
			# return self.mem[prev+l+1],self.input_rank
    	
		# print("\n")
		# print(self.mem_thr_tmp)
		# print("\n")
		# print(self.all_neuron_num)
		# print("\n")
		# print("divide",self.mem_thr_tmp[19]/self.all_neuron_num[19])

		# self.mem_thr_tmp = self.mem_thr_tmp.values
		# self.all_neuron_num = self.all_neuron_num.values

		# print(self.mem_thr_tmp/self.all_neuron_num)

		if(self.cal_neuron):
			self.mem_thr_tmp_conv =  list(self.mem_thr_tmp_conv.values())
			self.all_neuron_num_conv = list(self.all_neuron_num_conv.values())			
			self.mem_thr_tmp_linear =  list(self.mem_thr_tmp_linear.values())
			self.all_neuron_num_linear = list(self.all_neuron_num_linear.values())

			spike_rate_conv = np.array(self.mem_thr_tmp_conv)/np.array(self.all_neuron_num_conv)
			spike_rate_linear = np.array(self.mem_thr_tmp_linear)/np.array(self.all_neuron_num_linear)

			
			# print(spike_rate)

			return self.mem[prev+l+1],spike_rate_conv,spike_rate_linear

		


		return self.mem[prev+l+1]
