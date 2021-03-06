import os
import sys
import time
from six.moves import cPickle
import numpy as np
import scipy.ndimage as nd
from PIL import Image
import tensorflow as tf
import optparse
from dataset import dataset

"""
GAIN-GCAM
----------------------
This code implements the model described in the experiment section of GAIN(https://arxiv.org/pdf/1802.10171.pdf)
 * Segmentation model: Grad-CAM(ICCV'17)
 * Base model: VGG16 (remove 2 pooling layer)
"""

SAVER_PATH, PRED_PATH = "gain_gcam-saver", "gain_gcam-preds"

def parse_arg():
    parser = optparse.OptionParser()
    parser.add_option('-g', dest='gpu_id', default='0', help='specify to run on which GPU')
    parser.add_option('-f', dest='gpu_frac', default='0.49', help='specify the memory utilization of GPU')
    parser.add_option('-r', dest='restore_iter_id', default=None, help="continue training? default=False")
    parser.add_option('-a', dest='action', default='train', help="training or inference?")
    (options, args) = parser.parse_args()
    return options

class GAIN():
    def __init__(self,config):
        self.config = config
        # size of image(`input`)
        self.h, self.w = self.config.get("input_size", (321,321))
        # size of complement image(`input_c`)
        self.cw, self.ch = 321,321
        self.category_num, self.accum_num = self.config.get("category_num",21), self.config.get("accum_num",1)
        self.data, self.min_prob = self.config.get("data",None), self.config.get("min_prob",0.0001)
        self.net, self.loss, self.saver, self.weights, self.stride = {}, {}, {}, {}, {}
        self.trainable_list, self.lr_1_list, self.lr_2_list, self.lr_4_list, self.lr_8_list = [], [], [], [], []
        self.stride["input"] = 1
        self.stride["input_c"] = 1

    def build(self):
        if "output" not in self.net:
            with tf.name_scope("placeholder"):
                self.net["input"] = tf.placeholder(tf.float32,[None,self.h,self.w,self.config.get("input_channel",3)])
                self.net["label"] = tf.placeholder(tf.int32,[None,self.category_num])
                self.net["drop_prob"] = tf.placeholder(tf.float32)
            self.net["output"] = self.create_network()
        return self.net["output"]
    def create_network(self):
        if "init_model_path" in self.config: self.load_init_model()
        # path of `input` to VGG16
        with tf.name_scope("vgg16") as scope:
            block = self.build_block("input", [
                "conv1_1","relu1_1","conv1_2","relu1_2","pool1","conv2_1","relu2_1","conv2_2","relu2_2","pool2",
                "conv3_1","relu3_1","conv3_2","relu3_2","conv3_3","relu3_3","pool3",
                "conv4_1","relu4_1","conv4_2","relu4_2","conv4_3","relu4_3","pool4",
                "conv5_1","relu5_1","conv5_2","relu5_2","conv5_3","relu5_3","pool5"])
            last_layer = self.build_fc(block, ["fc6","relu6","drop6","fc7","relu7","drop7"])
            self.net[last_layer] = tf.reduce_sum(self.net[last_layer], axis=(1,2))
            fc = self.build_fc(last_layer, ["fc8"])
            # generate the attention map with Grad-CAM
            self.build_grad_cam(target="fc8", fmap="pool5")
        # path of `input_c` to VGG16
        with tf.name_scope("am") as scope:
            with tf.variable_scope(tf.get_variable_scope().name, reuse=tf.AUTO_REUSE) as var_scope:
                var_scope.reuse_variables()
                # generate `input_c`, which is the complement part of the image not selected by the attention map
                input_c = self.build_input_c("gcam", "input")
                block = self.build_block(input_c, [
                    "conv1_1","relu1_1","conv1_2","relu1_2","pool1","conv2_1","relu2_1","conv2_2","relu2_2","pool2",
                    "conv3_1","relu3_1","conv3_2","relu3_2","conv3_3","relu3_3","pool3",
                    "conv4_1","relu4_1","conv4_2","relu4_2","conv4_3","relu4_3","pool4",
                    "conv5_1","relu5_1","conv5_2","relu5_2","conv5_3","relu5_3","pool5"], is_exist=True)
                last_layer = self.build_fc(block, ["fc6","relu6","drop6","fc7","relu7","drop7"], is_exist=True)
                self.net[last_layer] = tf.reduce_sum(self.net[last_layer], axis=(1,2))
                fc = self.build_fc(last_layer, ["fc8"], is_exist=True)
        return self.net[fc]
    def build_block(self, last_layer, layer_lists, is_exist=False):
        input_layer = last_layer
        for layer in layer_lists:
            player = layer if not is_exist else '-'.join([input_layer, layer])
            with tf.name_scope(layer) as scope:
                if layer.startswith("conv"):
                    self.stride[player] = self.stride[last_layer]
                    weights, bias = self.get_weights_and_bias(layer, is_exist=is_exist)
                    self.net[player] = tf.nn.conv2d(self.net[last_layer], weights, strides=[1,1,1,1], padding="SAME", name="conv") if layer[4]!="5" else tf.nn.atrous_conv2d(self.net[last_layer], weights, rate=2, padding="SAME", name="conv")
                    self.net[player] = tf.nn.bias_add(self.net[player], bias, name="bias")
                elif layer.startswith("batch_norm"):
                    self.stride[player] = self.stride[last_layer]
                    self.net[player] = tf.contrib.layers.batch_norm(self.net[last_layer])
                elif layer.startswith("relu"):
                    self.stride[player] = self.stride[last_layer]
                    self.net[player] = tf.nn.relu(self.net[last_layer],name="relu")
                elif layer.startswith("pool"):
                    c, s = (1, [1,1,1,1]) if layer[4] in ["4","5"] else (2, [1,2,2,1])
                    self.stride[player] = c*self.stride[last_layer]
                    self.net[player] = tf.nn.max_pool(self.net[last_layer],ksize=[1,3,3,1],strides=s,padding="SAME",name="pool")
                else: raise Exception("Unimplemented layer: {}".format(layer))
                last_layer = player
        return last_layer
    def build_fc(self, last_layer, layer_lists, is_exist=False):
        input_layer = last_layer.split('-')[0]
        for layer in layer_lists:
            player = layer if not is_exist else '-'.join([input_layer, layer])
            with tf.name_scope(layer) as scope:
                if layer.startswith("fc"):
                    weights, bias = self.get_weights_and_bias(layer, is_exist=is_exist)
                    if layer.startswith("fc6"): self.net[player] = tf.nn.atrous_conv2d(self.net[last_layer], weights, rate=12, padding="SAME", name="conv")
                    elif layer.startswith("fc8"): self.net[player] = tf.matmul(self.net[last_layer], weights)
                    else: self.net[player] = tf.nn.conv2d(self.net[last_layer], weights, strides=[1,1,1,1], padding="SAME", name="conv")
                    self.net[player] = tf.nn.bias_add(self.net[player], bias, name="bias")
                elif layer.startswith("batch_norm"): self.net[player] = tf.contrib.layers.batch_norm(self.net[last_layer])
                elif layer.startswith("drop"): self.net[player] = tf.nn.dropout(self.net[last_layer], self.net["drop_prob"])
                elif layer.startswith("relu"): self.net[player] = tf.nn.relu(self.net[last_layer])
                else: raise Exception("Unimplemented layer: {}".format(layer))
                last_layer = player
        return last_layer
    def build_grad_cam(self, target, fmap):
        """
        Implement Grad-CAM(ICCV'17)
        -----------------------------------
        Input: predicted target Y[#class], feature map A[w/8,h/8]
        return: CAM[#class,w/8,h/8], where CAM[c,:,:] = ReLU(\sum_k alpha_k*A^k)
        """
        A, Y = self.net[fmap], self.net[target]
        cams = []
        for c in range(self.category_num):
            # calculate the importance of each feature map
            alpha = tf.reduce_sum(tf.gradients(Y[:,c], A)[0], axis=(1,2))
            # normalize alpha
            alpha = alpha/tf.reduce_sum(alpha, axis=(0,1))
            # linear combine the feature map to generate CAM
            cam_c = tf.reduce_sum(tf.reshape(tf.reshape(alpha, (-1,1))*tf.reshape(tf.transpose(A, [0,3,1,2]), (-1,41*41)), (-1,512,41*41)), axis=1)
            cams.append(tf.nn.relu(cam_c))
        cams = tf.reshape(tf.stack(cams, axis=2), (-1,41,41,self.category_num))
        self.net['gcam'] = cams
    def build_input_c(self, att_layer, img_layer, w=10, th=0.5):
        """
        Generate the image complement.
        ------------------------------------------------------------------------
        Input: image I[w,h,3], attention map A[w/8,h/8,#class],
        return: image complement I[w,h,#class], where I[:,:,c] = I[:,:,c]-I[:,:,c]*resize(A[:,:,c])
        """
        image, atts = tf.image.resize_bilinear(self.net[img_layer], (self.cw,self.ch)), tf.image.resize_bilinear(self.net[att_layer], (self.cw,self.ch))
        layer = "input_c"
        rst = []
        for att in tf.unstack(atts, axis=3):
            c = tf.expand_dims(image-tf.reshape(tf.multiply(tf.reshape(image, (-1,3)), tf.reshape(att, (-1,1))), (-1,self.cw,self.ch,3)), axis=1)
            c = tf.nn.sigmoid(w*(c-th)) # threshold masking
            rst.append(c)
        x = tf.stack(rst, axis=1)
        image_c = tf.reshape(x, (-1,self.cw,self.ch,3))
        self.net[layer] = image_c
        return layer
    def load_init_model(self):
        """Load the pre-trained VGG16 weight"""
        model_path = self.config["init_model_path"]
        self.init_model = np.load(model_path,encoding="latin1").item()
        print("load init model success: %s" % model_path)
    def restore_from_model(self, saver, model_path, checkpoint=False):
        assert self.sess is not None
        if checkpoint: saver.restore(self.sess, tf.train.get_checkpoint_state(model_path).model_checkpoint_path)
        else: saver.restore(self.sess, model_path)
    def get_weights_and_bias(self, layer, is_exist=False):
        if is_exist: return tf.get_variable(name="{}_weights".format(layer)), tf.get_variable(name="{}_bias".format(layer))
        if layer.startswith("conv"):
            shape = [3,3,0,0]
            if layer == "conv1_1": shape[2]=3
            else:
                shape[2] = min(64*self.stride[layer], 512)
                if layer in ["conv2_1","conv3_1","conv4_1"]: shape[2]=int(shape[2]/2)
            shape[3] = min(64*self.stride[layer], 512)
        if layer.startswith("fc"):
            if layer == "fc6": shape=[3,3,512,1024]
            elif layer == "fc7": shape=[1,1,1024,1024]
            elif layer == "fc8": shape=[1024,self.category_num]
        if "init_model_path" not in self.config:
            weights = tf.get_variable(name="{}_weights".format(layer), initializer=tf.random_normal_initializer(stddev=0.01), shape=shape)
            bias = tf.get_variable(name="{}_bias".format(layer), initializer=tf.constant_initializer(0), shape=[shape[-1]])
        else: # restroe from init.npy
            weights = tf.get_variable(name="{}_weights".format(layer), initializer=tf.contrib.layers.xavier_initializer(uniform=True) if layer=="fc8" else tf.constant_initializer(self.init_model[layer]["w"]), shape=shape)
            bias = tf.get_variable(name="{}_bias".format(layer), initializer=tf.constant_initializer(0) if layer=="fc8" else tf.constant_initializer(self.init_model[layer]["b"]), shape = [shape[-1]])
        self.weights[layer] = (weights, bias)
        if layer != "fc8":
            self.lr_1_list.append(weights)
            self.lr_2_list.append(bias)
        else: # the lr is larger in the last layer
            self.lr_4_list.append(weights)
            self.lr_8_list.append(bias)
        self.trainable_list.append(weights)
        self.trainable_list.append(bias)
        return weights, bias

    def get_cl_loss(self):
        """Loss of Multi-Label Classification"""
        return tf.reduce_mean(tf.reduce_sum([tf.nn.sigmoid_cross_entropy_with_logits(logits=self.net["fc8"][:,c], labels=tf.cast(self.net["label"][:,c], tf.float32)) for c in range(self.category_num)]))
    def get_am_loss(self):
        """Implements the Attention Mining Loss described in GAIN
        ---------------------------------------------------------
        return the sum of class scores given the complement image `input_c`
        """
        x = tf.reshape(tf.nn.sigmoid(self.net["input_c-fc8"]), (-1, self.category_num, category_num))
        score = tf.stack([x[:,c,c] for c in range(self.category_num)], axis=1)
        return tf.reduce_mean(tf.reduce_sum(score, axis=1) / tf.cast(tf.reduce_sum(self.net["label"], axis=1), tf.float32))
    
    def add_loss_summary(self):
        tf.summary.scalar('cl-loss', self.loss["loss_cl"])
        tf.summary.scalar('am-loss', self.loss["loss_am"])
        tf.summary.scalar('l2', self.loss["total"]-self.loss["norm"])
        tf.summary.scalar('total', self.loss["total"])
        self.merged = tf.summary.merge_all()
        self.writer = tf.summary.FileWriter(os.path.join(SAVER_PATH, 'sum'))

    def optimize(self, base_lr, momentum, weight_decay):
        self.loss["loss_cl"] = self.get_cl_loss()
        self.loss["loss_am"] = self.get_am_loss()
        self.loss["norm"] = self.loss["loss_cl"] + self.loss["loss_am"]
        self.loss["l2"] = tf.reduce_sum([tf.nn.l2_loss(self.weights[layer][0]) for layer in self.weights], axis=0)
        self.loss["total"] = self.loss["norm"] + weight_decay*self.loss["l2"]
        self.net["lr"] = tf.Variable(base_lr, trainable=False, dtype=tf.float32)
        opt = tf.train.AdamOptimizer(self.net["lr"],momentum)
        gradients = opt.compute_gradients(self.loss["total"],var_list=self.trainable_list)
        self.grad = {}
        self.net["accum_gradient"] = []
        self.net["accum_gradient_accum"] = []
        new_gradients = []
        for (g,v) in gradients:
            if v in self.lr_2_list: g = 2*g
            if v in self.lr_4_list: g = 4*g
            if v in self.lr_8_list: g = 8*g
            self.net["accum_gradient"].append(tf.Variable(tf.zeros_like(g),trainable=False))
            self.net["accum_gradient_accum"].append(self.net["accum_gradient"][-1].assign_add(g/self.accum_num, use_locking=True))
            new_gradients.append((self.net["accum_gradient"][-1],v))

        self.net["accum_gradient_clean"] = [g.assign(tf.zeros_like(g)) for g in self.net["accum_gradient"]]
        self.net["accum_gradient_update"]  = opt.apply_gradients(new_gradients)

    def train(self, base_lr, weight_decay, momentum, batch_size, epoches, gpu_frac):
        gpu_options = tf.ConfigProto(gpu_options=tf.GPUOptions(per_process_gpu_memory_fraction=gpu_frac))
        self.sess = tf.Session(config=gpu_options)
        x, _, y, c, id_of_image, iterator_train = self.data.next_batch(category="train",batch_size=batch_size,epoches=-1)
        self.build()
        self.optimize(base_lr,momentum, weight_decay)
        self.saver["norm"] = tf.train.Saver(max_to_keep=2,var_list=self.trainable_list)
        self.saver["lr"] = tf.train.Saver(var_list=self.trainable_list)
        self.saver["best"] = tf.train.Saver(var_list=self.trainable_list,max_to_keep=2)
        self.add_loss_summary()

        with self.sess.as_default():
            self.sess.run(tf.global_variables_initializer())
            self.sess.run(tf.local_variables_initializer())
            self.sess.run(iterator_train.initializer)
            if self.config.get("model_path",False) is not False: self.restore_from_model(self.saver["norm"], self.config.get("model_path"), checkpoint=False)
            start_time = time.time()
            print("start_time: {}\nconfig -- lr:{} weight_decay:{} momentum:{} batch_size:{} epoches:{}".format(start_time, base_lr, weight_decay, momentum, batch_size, epoches))
            
            epoch, i, iterations_per_epoch_train = 0.0, 0, self.data.get_data_len()//batch_size
            while epoch < epoches:
                if i == 0: self.sess.run(tf.assign(self.net["lr"],base_lr))
                if i == 10*iterations_per_epoch_train:
                    new_lr = 1e-4
                    self.saver["lr"].save(self.sess, os.path.join(self.config.get("saver_path",SAVER_PATH),"lr-%f"%base_lr), global_step=i)
                    self.sess.run(tf.assign(self.net["lr"], new_lr))
                    base_lr = new_lr
                if i == 20*iterations_per_epoch_train:
                    new_lr = 1e-5
                    self.saver["lr"].save(self.sess, os.path.join(self.config.get("saver_path",SAVER_PATH),"lr-%f"%base_lr), global_step=i)
                    self.sess.run(tf.assign(self.net["lr"],new_lr))
                    base_lr = new_lr
                data_x, data_y, _, data_id_of_image = self.sess.run([x, y, c, id_of_image])
                params = {self.net["input"]:data_x, self.net["label"]:data_y, self.net["drop_prob"]:0.5}
                self.sess.run(self.net["accum_gradient_accum"], feed_dict=params)
                if i % self.accum_num == self.accum_num-1:
                    _, _ = self.sess.run(self.net["accum_gradient_update"]), self.sess.run(self.net["accum_gradient_clean"])
                if i%500 == 0:
                    summary, loss_cl, loss_am, loss_l2, loss_total, lr = self.sess.run([self.merged, self.loss["loss_cl"], self.loss["loss_am"], self.loss["l2"], self.loss["total"], self.net["lr"]], feed_dict=params)
                    print("{:.1f}th epoch, {}iters, lr={:.5f}, loss={:.5f}+{:.5f}+{:.5f}={:.5f}".format(epoch, i, lr, loss_cl, loss_am, weight_decay*loss_l2, loss_total))
                    self.writer.add_summary(summary, global_step=i)
                if i%3000 == 2999:
                    self.saver["norm"].save(self.sess, os.path.join(self.config.get("saver_path",SAVER_PATH),"norm"), global_step=i)
                i+=1
                epoch = i/iterations_per_epoch_train
            end_time = time.time()
            print("end_time:{}\nduration time:{}".format(end_time, (end_time-start_time)))
    def inference(self, gpu_frac, eps=1e-5):
        if not os.path.exists(PRED_PATH): os.makedirs(PRED_PATH)
        #Dump the predicted mask as numpy array to disk
        gpu_options = tf.ConfigProto(gpu_options=tf.GPUOptions(per_process_gpu_memory_fraction=gpu_frac))
        self.sess = tf.Session(config=gpu_options)
        x, gt, _, _, id_of_image, iterator_train = self.data.next_batch(category="train",batch_size=1,epoches=-1)
        self.build()
        self.saver["norm"] = tf.train.Saver(max_to_keep=2,var_list=self.trainable_list)
        with self.sess.as_default():
            self.sess.run(tf.global_variables_initializer())
            self.sess.run(tf.local_variables_initializer())
            self.sess.run(iterator_train.initializer)
            if self.config.get("model_path",False) is not False: self.restore_from_model(self.saver["norm"], self.config.get("model_path"), checkpoint=False)
            epoch, i, iterations_per_epoch_train = 0.0, 0, self.data.get_data_len()
            while epoch < 1:
                data_x, data_gt, img_id = self.sess.run([x, gt, id_of_image])
                cimg_id = img_id[0].decode("utf-8")
                preds = self.sess.run(self.net["gcam"], feed_dict={self.net["input"]:data_x, self.net["drop_prob"]:0.5})
                for pred in preds:
                    img = Image.open("data/VOCdevkit/VOC2012/JPEGImages/{}.jpg".format(cimg_id)).resize((321,321), Image.ANTIALIAS)
                    scores_exp = np.exp(pred-np.max(pred, axis=2, keepdims=True))
                    probs = scores_exp/np.sum(scores_exp, axis=2, keepdims=True)
                    probs = nd.zoom(probs, (321/probs.shape[0], 321/probs.shape[1], 1.0), order=1)
                    probs[probs<eps] = eps
                    mask = np.argmax(probs, axis=2)
                    cPickle.dump(mask, open('{}/{}.pkl'.format(PRED_PATH, img_id[0].decode("utf-8")), 'wb'))
                i+=1
                epoch = i/iterations_per_epoch_train


if __name__ == "__main__":
    opt = parse_arg()
    os.environ["CUDA_VISIBLE_DEVICES"] = opt.gpu_id
    batch_size = 1 # actual batch size=batch_size*accum_num
    input_size, category_num, epoches = (321,321), 21, 10
    data = dataset({"batch_size":batch_size, "input_size":input_size, "epoches":epoches, "category_num":category_num, "categorys":["train"]})
    if opt.restore_iter_id == None: gain = GAIN({"data":data, "batch_size":batch_size, "input_size":input_size, "epoches":epoches, "category_num":category_num, "init_model_path":"./model/init.npy", "accum_num":16})
    else: gain = GAIN({"data":data, "batch_size":batch_size, "input_size":input_size, "epoches":epoches, "category_num":category_num, "model_path":"{}/norm-{}".format(SAVER_PATH, opt.restore_iter_id), "accum_num":16})
    if opt.action == 'train':
        gain.train(base_lr=1e-4, weight_decay=5e-5, momentum=0.9, batch_size=batch_size, epoches=epoches, gpu_frac=float(opt.gpu_frac))
    elif opt.action == 'inference':
        gain.inference(gpu_frac=float(opt.gpu_frac))