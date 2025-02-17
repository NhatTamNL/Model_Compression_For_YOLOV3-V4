#python3 train.py --data data_firev6/yolo.data --batch-size 16 
# --weights data_firev6/backup/yolov3-tiny_90000.weights --cfg data_firev6/yolov3-tiny.cfg --img-size 416  --epochs 100 --device 0
import argparse

import torch.distributed as dist
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.tensorboard import SummaryWriter

import test  # import test.py to get mAP after each epoch
from models import *
from utils.datasets import *
from utils.utils import *
from utils.prune_utils import *
from utils.torch_utils import *
from utils.torch_utils import select_device
import math

mixed_precision = True
try:  # Mixed precision training https://github.com/NVIDIA/apex
    from apex import amp
except:
    print('Apex recommended for faster mixed precision training: https://github.com/NVIDIA/apex')
    mixed_precision = False  # not installed

wdir = 'weights' + os.sep  # weights dir
last = wdir + 'last.pt'
best = wdir + 'best.pt'
results_file = 'results.txt'


# Hyperparameters (j-series, 50.5 mAP yolov3-320) evolved by @ktian08 https://github.com/ultralytics/yolov3/issues/310
hyp = {'giou': 3.582,  # giou loss gain
       'cls': 37.76,  # cls loss gain  (CE=~1.0, uCE=~20)
       'cls_pw': 1.146,  # cls BCELoss positive_weight
       'obj': 64.35,  # obj loss gain (*=80 for uBCE with 80 classes)
       'obj_pw': 1.11,  # obj BCELoss positive_weight
       'iou_t': 0.20,  # iou training threshold
       'lr0': 0.002610,  # initial learning rate (SGD=1E-3, Adam=9E-5)
       'lrf': 0.0005,  # final LambdaLR learning rate = lr0 * (10 ** lrf)
       'momentum': 0.937,  # SGD momentum
       'weight_decay': 0.0005,  # optimizer weight decay
       'fl_gamma': 0.0,  # focal loss gamma
       'hsv_h': 0.0138,  # image HSV-Hue augmentation (fraction)
       'hsv_s': 0.678,  # image HSV-Saturation augmentation (fraction)
       'hsv_v': 0.36,  # image HSV-Value augmentation (fraction)
       'degrees': 1.113*0,  # image rotation (+/- deg)
       'translate': 0.06797*0,  # image translation (+/- fraction)
       'scale': 0.1059*0,  # image scale (+/- gain)
       'shear': 0.5768*0}  # image shear (+/- deg)

'''
# Hyperparameters
hyp = {'giou': 1.54,  # giou loss gain
       'cls': 27.4,  # cls loss gain
       'cls_pw': 1.44,  # cls BCELoss positive_weight
       'obj': 21.3,  # obj loss gain (*=img_size/320 if img_size != 320)
       'obj_pw': 3.9,  # obj BCELoss positive_weight
       'iou_t': 0.20,  # iou training threshold
       # 'lr0': 0.01,  # initial learning rate (SGD=5E-3, Adam=5E-4)
       'lr0': 0.0002,  # initial learning rate (SGD=5E-3, Adam=5E-4)
       'lrf': 0.0005,  # final learning rate (with cos scheduler)
       'momentum': 0.98,  # SGD momentum
       'weight_decay': 0.0005,  # optimizer weight decay
       'fl_gamma': 0.0,  # focal loss gamma (efficientDet default is gamma=1.5)
       'hsv_h': 0.0138,  # image HSV-Hue augmentation (fraction)
       'hsv_s': 0.678,  # image HSV-Saturation augmentation (fraction)
       'hsv_v': 0.36,  # image HSV-Value augmentation (fraction)
       'degrees': 1.98 * 0,  # image rotation (+/- deg)
       'translate': 0.05 * 0,  # image translation (+/- fraction)
       'scale': 0.05 * 0,  # image scale (+/- gain)
       'shear': 0.641 * 0}  # image shear (+/- deg)

'''



# Overwrite hyp with hyp*.txt (optional)
f = glob.glob('hyp*.txt')
if f:
    print('Using %s' % f[0])
    for k, v in zip(hyp.keys(), np.loadtxt(f[0])):
        hyp[k] = v

# Print focal loss if gamma > 0
if hyp['fl_gamma']:
    print('Using FocalLoss(gamma=%g)' % hyp['fl_gamma'])


def train(hyp):
    cfg = opt.cfg
    t_cfg = opt.t_cfg  # teacher model cfg for knowledge distillation
    data = opt.data
    epochs = opt.epochs  # 500200 batches at bs 64, 117263 images = 273 epochs
    batch_size = opt.batch_size
    accumulate = max(round(64 / batch_size), 1)  # accumulate n times before optimizer update (bs 64)
    if opt.quantized != 0:
        weights = "weights/last.pt"
    else:
        weights = opt.weights  # initial training weights

    t_weights = opt.t_weights  # teacher model weights
    imgsz_min, imgsz_max, imgsz_test = opt.img_size  # img sizes (min, max, test)

    # Image Sizes
    gs = 32  # (pixels) grid size
    #gs = 16
    assert math.fmod(imgsz_min, gs) == 0, '--img-size %g must be a %g-multiple' % (imgsz_min, gs)
    opt.multi_scale |= imgsz_min != imgsz_max  # multi if different (min, max)
    if opt.multi_scale:
        if imgsz_min == imgsz_max:
            imgsz_min //= 1.5
            imgsz_max //= 0.667
        grid_min, grid_max = imgsz_min // gs, imgsz_max // gs
        imgsz_min, imgsz_max = int(grid_min * gs), int(grid_max * gs)
    img_size = imgsz_max  # initialize with max size

    # Configure run
    # init_seeds()
    seed_torch()   #  modifiey on 2020 dec-31;
    data_dict = parse_data_cfg(data)
    train_path = data_dict['train']
    test_path = data_dict['valid']
    nc = 1 if opt.single_cls else int(data_dict['classes'])  # number of classes
    hyp['cls'] *= nc / 80  # update coco-tuned hyp['cls'] to current dataset

    # Remove previous results
    for f in glob.glob('*_batch*.jpg') + glob.glob(results_file):
        os.remove(f)

    # Initialize model
    model = Darknet(cfg, quantized=opt.quantized, a_bit=opt.a_bit, w_bit=opt.w_bit, BN_Fold=opt.BN_Fold,
                    FPGA=opt.FPGA).to(device)
    if t_cfg:
        t_model = Darknet(t_cfg).to(device)



    # Optimizer
    pg0, pg1, pg2 = [], [], []  # optimizer parameter groups
    for k, v in dict(model.named_parameters()).items():
        if '.bias' in k:
            pg2 += [v]  # biases
        elif 'Conv2d.weight' in k:
            pg1 += [v]  # apply weight_decay
        else:
            pg0 += [v]  # all else

    if opt.adam:
        # hyp['lr0'] *= 0.1  # reduce lr (i.e. SGD=5E-3, Adam=5E-4)
        optimizer = optim.Adam(pg0, lr=hyp['lr0'])
        # optimizer = AdaBound(pg0, lr=hyp['lr0'], final_lr=0.1)
    else:
        optimizer = optim.SGD(pg0, lr=hyp['lr0'], momentum=hyp['momentum'], nesterov=True)
    optimizer.add_param_group({'params': pg1, 'weight_decay': hyp['weight_decay']})  # add pg1 with weight_decay
    optimizer.add_param_group({'params': pg2})  # add pg2 (biases)
    print('Optimizer groups: %g .bias, %g Conv2d.weight, %g other' % (len(pg2), len(pg1), len(pg0)))
    del pg0, pg1, pg2

    print('<.....................using fencemask.......................>')
    seed = int(img_size / 32)
    fencemask = FenceMask(seed, seed * 3, seed * 4, seed * 8, [0, 0, 0], 0.8)
    max_epoch = int(epochs * 0.8)
    start_epoch = 0
    best_fitness = 0.0
    if weights != 'None':
        attempt_download(weights)
        if weights.endswith('.pt'):  # pytorch format
            # possible weights are '*.pt', 'yolov3-spp.pt', 'yolov3-tiny.pt' etc.
            chkpt = torch.load(weights, map_location=device)

            # load model
            try:
                chkpt['model'] = {k: v for k, v in chkpt['model'].items() if model.state_dict()[k].numel() == v.numel()}
                model.load_state_dict(chkpt['model'], strict=False)
            except KeyError as e:
                s = "%s is not compatible with %s. Specify --weights '' or specify a --cfg compatible with %s. " \
                    "See https://github.com/ultralytics/yolov3/issues/657" % (opt.weights, opt.cfg, opt.weights)
                raise KeyError(s) from e

            # load optimizer
            if chkpt['optimizer'] is not None:
                optimizer.load_state_dict(chkpt['optimizer'])
                best_fitness = chkpt['best_fitness']

            # load results
            if chkpt.get('training_results') is not None:
                with open(results_file, 'w') as file:
                    file.write(chkpt['training_results'])  # write results.txt

            start_epoch = chkpt['epoch'] + 1
            del chkpt

        elif len(weights) > 0:  # darknet format
            # possible weights are '*.weights', 'yolov3-tiny.conv.15',  'darknet53.conv.74' etc.
            load_darknet_weights(model, weights, pt=opt.pt, BN_Fold=opt.BN_Fold)
    if t_cfg:
        if t_weights.endswith('.pt'):
            t_model.load_state_dict(torch.load(t_weights, map_location=device)['model'])
        elif t_weights.endswith('.weights'):
            load_darknet_weights(t_model, t_weights)
        else:
            raise Exception('pls provide proper teacher weights for knowledge distillation')
        if not mixed_precision:
            t_model.eval()
        print('<.....................using knowledge distillation.......................>')
        print('teacher model:', t_weights, '\n')
    # Mixed precision training https://github.com/NVIDIA/apex
    if mixed_precision:
        model, optimizer = amp.initialize(model, optimizer, opt_level='O1', verbosity=0)

    # Scheduler https://arxiv.org/pdf/1812.01187.pdf
    lf = lambda x: (((1 + math.cos(x * math.pi / epochs)) / 2) ** 1.0) * 0.95 + 0.05  # cosine
    scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)
    scheduler.last_epoch = start_epoch - 1  # see link below
    # https://discuss.pytorch.org/t/a-problem-occured-when-resuming-an-optimizer/28822


    # Initialize distributed training
    if device.type != 'cpu' and torch.cuda.device_count() > 1 and torch.distributed.is_available():
        dist.init_process_group(backend='nccl',  # 'distributed backend'
                                init_method='tcp://127.0.0.1:9999',  # distributed training init method
                                world_size=1,  # number of nodes for distributed training
                                rank=0)  # distributed training node rank
        model = torch.nn.parallel.DistributedDataParallel(model, find_unused_parameters=True)
        model.yolo_layers = model.module.yolo_layers  # move yolo layer indices to top level

    # Dataset
    dataset = LoadImagesAndLabels(train_path, img_size, batch_size,
                                  augment=True,
                                  hyp=hyp,  # augmentation hyperparameters
                                  rect=opt.rect,  # rectangular training
                                  cache_images=opt.cache_images,
                                  single_cls=opt.single_cls)




    # 获得要剪枝的层
    if hasattr(model, 'module'):
        print('muti-gpus sparse')
        if opt.prune == 0:
            print('normal sparse training ')
            _, _, prune_idx = parse_module_defs(model.module.module_defs)
        elif opt.prune == 1:
            print('shortcut sparse training')
            _, _, prune_idx, _, _ = parse_module_defs2(model.module.module_defs)
        elif opt.prune == 2:
            print('layer sparse training')
            _, _, prune_idx = parse_module_defs4(model.module.module_defs)


    else:
        print('single-gpu sparse')
        if opt.prune == 0:
            print('normal sparse training')
            _, _, prune_idx = parse_module_defs(model.module_defs)
        elif opt.prune == 1:
            print('shortcut sparse training')
            _, _, prune_idx, _, _ = parse_module_defs2(model.module_defs)
        elif opt.prune == 2:
            print('layer sparse training')
            _, _, prune_idx = parse_module_defs4(model.module_defs)

    # Dataloader
    batch_size = min(batch_size, len(dataset))
    nw = min([os.cpu_count(), batch_size if batch_size > 1 else 0, 8])  # number of workers
    dataloader = torch.utils.data.DataLoader(dataset,
                                             batch_size=batch_size,
                                             num_workers=nw,
                                             shuffle=not opt.rect,  # Shuffle=True unless rectangular training is used
                                             pin_memory=True,
                                             collate_fn=dataset.collate_fn)

    # Testloader
    testloader = torch.utils.data.DataLoader(LoadImagesAndLabels(test_path, imgsz_test, batch_size,
                                                                 hyp=hyp,
                                                                 rect=True,
                                                                 cache_images=opt.cache_images,
                                                                 single_cls=opt.single_cls),
                                             batch_size=batch_size,
                                             num_workers=nw,
                                             pin_memory=True,
                                             collate_fn=dataset.collate_fn)

    if opt.sr:
     for idx in prune_idx:
            if hasattr(model, 'module'):
                bn_weights = gather_bn_weights(model.module.module_list, [idx])
            else:
                bn_weights = gather_bn_weights(model.module_list, [idx])
            tb_writer.add_histogram('before_train_perlayer_bn_weights/hist', bn_weights.numpy(), idx, bins='doane')
    # Model parameters
    model.nc = nc  # attach number of classes to model
    model.hyp = hyp  # attach hyperparameters to model
    model.gr = 1.0  # giou loss ratio (obj_loss = 1.0 or giou)
    model.class_weights = labels_to_class_weights(dataset.labels, nc).to(device)  # attach class weights

    # Model EMA
    if opt.ema:
        ema = ModelEMA(model)

    # Start training
    nb = len(dataloader)  # number of batches
    n_burn = max(3 * nb, 500)  # burn-in iterations, max(3 epochs, 500 iterations)
    maps = np.zeros(nc)  # mAP per class
    # torch.autograd.set_detect_anomaly(True)
    results = (0, 0, 0, 0, 0, 0, 0)  # 'P', 'R', 'mAP', 'F1', 'val GIoU', 'val Objectness', 'val Classification'
    t0 = time.time()
    print('Image sizes %g - %g train, %g test' % (imgsz_min, imgsz_max, imgsz_test))
    print('Using %g dataloader workers' % nw)
    print('Starting training for %g epochs...' % epochs)
    for epoch in range(start_epoch, epochs):  # epoch ------------------------------------------------------------------
        fencemask.set_prob(epoch, max_epoch)
        # gridmask.set_prob(epoch, max_epoch)
        model.train()
        print("learning rate lr: {:.6f}".format(optimizer.param_groups[0]['lr']))
        # 稀疏化标志
        sr_flag = get_sr_flag(epoch, opt.sr)

        # Update image weights (optional)
        if dataset.image_weights:
            w = model.class_weights.cpu().numpy() * (1 - maps) ** 2  # class weights
            image_weights = labels_to_image_weights(dataset.labels, nc=nc, class_weights=w)
            dataset.indices = random.choices(range(dataset.n), weights=image_weights, k=dataset.n)  # rand weighted idx
        mloss = torch.zeros(4).to(device)  # mean losses
        print(('\n' + '%10s' * 8) % ('Epoch', 'gpu_mem', 'GIoU', 'obj', 'cls', 'total', 'targets', 'img_size'))
        pbar = tqdm(enumerate(dataloader), total=nb)  # progress bar
        for i, (imgs, targets, paths, _) in pbar:  # batch -------------------------------------------------------------
            ni = i + nb * epoch  # number integrated batches (since train start)
            imgs = imgs.to(device).float() / 255.0  # uint8 to float32, 0 - 255 to 0.0 - 1.0

            # Burn-in
            if ni <= n_burn:
                xi = [0, n_burn]  # x interp
                model.gr = np.interp(ni, xi, [0.0, 1.0])  # giou loss ratio (obj_loss = 1.0 or giou)
                accumulate = max(1, np.interp(ni, xi, [1, 64 / batch_size]).round())
                for j, x in enumerate(optimizer.param_groups):
                    # bias lr falls from 0.1 to lr0, all other lrs rise from 0.0 to lr0
                    x['lr'] = np.interp(ni, xi, [0.1 if j == 2 else 0.0, x['initial_lr'] * lf(epoch)])
                    x['weight_decay'] = np.interp(ni, xi, [0.0, hyp['weight_decay'] if j == 1 else 0.0])
                    if 'momentum' in x:
                        x['momentum'] = np.interp(ni, xi, [0.9, hyp['momentum']])

            # Multi-Scale
            if opt.multi_scale:
                if ni / accumulate % 1 == 0:  #  adjust img_size (67% - 150%) every 1 batch
                    img_size = random.randrange(grid_min, grid_max + 1) * gs
                sf = img_size / max(imgs.shape[2:])  # scale factor
                if sf != 1:
                    ns = [math.ceil(x * sf / gs) * gs for x in imgs.shape[2:]]  # new shape (stretched to 32-multiple)
                    imgs = F.interpolate(imgs, size=ns, mode='bilinear', align_corners=False)
            # Forward
            imgs = fencemask(imgs)
            # imgs = gridmask(imgs)
            targets = targets.to(device)
            pred, feature_s = model(imgs)
            # Loss
            loss, loss_items = compute_loss(pred, targets, model)
            if not torch.isfinite(loss):
                print('WARNING: non-finite loss, ending training , loss between target and pred', loss_items)
                return results


            attention_loss = 0
            if t_cfg  and  opt.AT_str != -1:
                if mixed_precision:
                    with torch.no_grad():
                        output_t, feature_t = t_model(imgs)
                else:
                    _, output_t, feature_t = t_model(imgs)
                if opt.AT_str == 1:
                    attention_loss =  compute_lost_AT(model, targets, pred, output_t, feature_s, feature_t,
                                                   imgs.size(0))
                elif opt.AT_str == 2:
                    attention_loss = compute_lost_group_AT(model, targets, pred, output_t, feature_s, feature_t,
                                                     imgs.size(0))

                elif opt.AT_str == 3:
                    attention_loss = compute_lost_group_AT_KD(model, targets, pred, output_t, feature_s, feature_t,
                                                     imgs.size(0))
                elif opt.AT_str == 4:
                    attention_loss = compute_lost_group_AT_KD_for_v3_mobilenet(model, targets, pred, output_t, feature_s, feature_t,
                                                     imgs.size(0))
                elif opt.AT_str == 5:
                    attention_loss = compute_lost_group_AT_KD_for_v3_darknet53(model, targets, pred, output_t, feature_s, feature_t,
                                                     imgs.size(0))



                elif opt.AT_str == 10:
                    attention_loss = compute_lost_fine_grained_group_AT_KD(model, targets, pred, output_t, feature_s, feature_t,
                                                     batch_size,img_size)

                else:
                    print("please select attention transfer  strategy!")
                loss =  loss +  attention_loss
                if not torch.isfinite(loss):
                    print('WARNING: non-finite attention  transfer loss, ending training ', )
                    return results


            soft_target = 0
            if t_cfg  and  opt.KDstr != -1:
                if mixed_precision:
                    with torch.no_grad():
                        output_t, feature_t = t_model(imgs)
                else:
                    _, output_t, feature_t = t_model(imgs)
                if opt.KDstr == 1:
                    soft_target = compute_lost_KD(pred, output_t, model.nc, imgs.size(0))
                elif opt.KDstr == 2:
                    soft_target, reg_ratio = compute_lost_KD2(model, targets, pred, output_t)
                elif opt.KDstr == 3:
                    soft_target = compute_lost_KD3(model, targets, pred, output_t)
                elif opt.KDstr == 4:
                    soft_target = compute_lost_KD4(model, targets, pred, output_t, feature_s, feature_t,
                                                   imgs.size(0))
                elif opt.KDstr == 5:
                    soft_target = compute_lost_KD5(model, targets, pred, output_t, feature_s, feature_t,
                                                   imgs.size(0),
                                                   img_size)

                elif opt.KDstr == 6:
                    soft_target = compute_lost_KD6(model, targets, pred, output_t, imgs.size(0))
                else:
                    print("please select KD strategy!")
                loss =  loss + soft_target

                # Backward
            loss *= batch_size / 64  # scale loss
            if mixed_precision:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            # 对要剪枝层的γ参数稀疏化

            if hasattr(model, 'module'):
                if opt.prune != -1:
                    BNOptimizer.updateBN(sr_flag, model.module.module_list, opt.s, prune_idx)
            else:
                '''
                idx2mask = None
                if opt.prune != -1:
                    BNOptimizer.updateBN(sr_flag, model.module_list, opt.s, prune_idx,epoch, idx2mask, opt )
                '''
                idx2mask = None
                if  opt.prune == 1 and epoch > opt.epochs * 0.4:
                    idx2mask = get_mask2(model, prune_idx, 0.65)
                    # BNOptimizer.updateBN(sr_flag, model.module_list, opt.s, prune_idx)
                    BNOptimizer.updateBN(sr_flag, model.module_list, opt.s, prune_idx, epoch, idx2mask, opt)

                elif  opt.prune == 1 and epoch <= opt.epochs * 0.4:
                    BNOptimizer.updateBN(sr_flag, model.module_list, opt.s, prune_idx, epoch, idx2mask, opt)

            '''
 
            '''
            # Optimize
            if ni % accumulate == 0:
                optimizer.step()
                optimizer.zero_grad()
                if opt.ema:
                    ema.update(model)

            # Print
            mloss = (mloss * i + loss_items) / (i + 1)  # update mean losses
            mem = '%.3gG' % (torch.cuda.memory_reserved() / 1E9 if torch.cuda.is_available() else 0)  # (GB)
            s = ('%10s' * 2 + '%10.3g' * 6) % ('%g/%g' % (epoch, epochs - 1), mem, *mloss, len(targets), img_size)
            pbar.set_description(s)

            # Plot
            if ni < 1:
                f = 'train_batch%g.jpg' % i  # filename
                res = plot_images(images=imgs, targets=targets, paths=paths, fname=f)
                if tb_writer:
                    tb_writer.add_image(f, res, dataformats='HWC', global_step=epoch)
                    # tb_writer.add_graph(model, imgs)  # add model to tensorboard

            # end batch ------------------------------------------------------------------------------------------------

        # Update scheduler
        scheduler.step()

        # Process epoch results
        if opt.ema:
            ema.update_attr(model)

            if hasattr(model, 'module'):
                module_defs, module_list = ema.eam.module.module_defs, ema.eam.module.module_list
            else:
                module_defs, module_list = ema.eam.module_defs, ema.eam.module_list

            for i, (mdef, module) in enumerate(zip(module_defs, module_list)):
                if mdef['type'] == 'yolo':
                    yolo_layer = module
                    yolo_layer.nx, yolo_layer.ny = 0, 0
        if hasattr(model, 'module'):
            module_defs, module_list = model.module.module_defs, model.module.module_list
        else:
            module_defs, module_list = model.module_defs, model.module_list
        for i, (mdef, module) in enumerate(zip(module_defs, module_list)):
            if mdef['type'] == 'yolo':
                yolo_layer = module
                yolo_layer.nx, yolo_layer.ny = 0, 0

        final_epoch = epoch + 1 == epochs
        if not opt.notest or final_epoch:  # Calculate mAP
            is_coco = any([x in data for x in ['coco.data', 'coco2014.data', 'coco2017.data']]) and model.nc == 80
            results, maps = test.test(cfg,
                                      data,
                                      batch_size=batch_size,
                                      imgsz=imgsz_test,
                                      model=ema.ema if opt.ema else model,
                                      save_json=final_epoch and is_coco,
                                      single_cls=opt.single_cls,
                                      dataloader=testloader,
                                      multi_label=ni > n_burn,
                                      quantized=opt.quantized,
                                      a_bit=opt.a_bit,
                                      w_bit=opt.w_bit,
                                      BN_Fold=opt.BN_Fold,
                                      FPGA=opt.FPGA)

        # Write
        with open(results_file, 'a') as f:
            f.write(s + '%10.3g' * 7 % results + '\n')  # P, R, mAP, F1, test_losses=(GIoU, obj, cls)
        if len(opt.name) and opt.bucket:
            os.system('gsutil cp results.txt gs://%s/results/results%s.txt' % (opt.bucket, opt.name))

        # Tensorboard
        if tb_writer:
            tags = ['train/giou_loss', 'train/obj_loss', 'train/cls_loss',
                    'metrics/precision', 'metrics/recall', 'metrics/mAP_0.5', 'metrics/F1',
                    'val/giou_loss', 'val/obj_loss', 'val/cls_loss']
            for x, tag in zip(list(mloss[:-1]) + list(results), tags):
                tb_writer.add_scalar(tag, x, epoch)
            if opt.sr:
                 if hasattr(model, 'module'):
                    bn_weights = gather_bn_weights(model.module.module_list, [idx])
                 else:
                    bn_weights = gather_bn_weights(model.module_list, [idx])
                 tb_writer.add_histogram('after_sparse_train_every_epoch_total_layers_of_bn_weights/hist', bn_weights.numpy(), epoch, bins='doane')


        # Update best mAP
        fi = fitness(np.array(results).reshape(1, -1))  # fitness_i = weighted combination of [P, R, mAP, F1]
        if fi > best_fitness:
            best_fitness = fi

        # Save model
        save = (not opt.nosave) or (final_epoch and not opt.evolve)
        if opt.ema:
            if hasattr(model, 'module'):
                model_temp = ema.ema.module.state_dict()
            else:
                model_temp = ema.ema.state_dict()
        else:
            if hasattr(model, 'module'):
                model_temp = model.module.state_dict()
            else:
                model_temp = model.state_dict()
        if save:
            with open(results_file, 'r') as f:  # create checkpoint
                chkpt = {'epoch': epoch,
                         'best_fitness': best_fitness,
                         'training_results': f.read(),
                         'model': model_temp,
                         'optimizer': None if final_epoch else optimizer.state_dict()}

            # Save last, best and delete
            torch.save(chkpt, last)
            if (best_fitness == fi) and not final_epoch:
                torch.save(chkpt, best)
            del chkpt

        # end epoch ----------------------------------------------------------------------------------------------------
    if opt.sr:
      for  idx_s in prune_idx:
        bn_weights = gather_bn_weights(model.module_list, [idx_s])
        tb_writer.add_histogram('after_sparse_train_perlayer_bn_weights/hist',bn_weights.numpy(), idx_s, bins='doane')


    # end training

    n = opt.name
    if len(n):
        n = '_' + n if not n.isnumeric() else n
        fresults, flast, fbest = 'results%s.txt' % n, wdir + 'last%s.pt' % n, wdir + 'best%s.pt' % n
        for f1, f2 in zip([wdir + 'v4_tiny_416_map61.pt', wdir + 'best.pt', 'results.txt'], [flast, fbest, fresults]):
            if os.path.exists(f1):
                os.rename(f1, f2)  # rename
                ispt = f2.endswith('.pt')  # is *.pt
                strip_optimizer(f2) if ispt else None  # strip optimizer
                os.system('gsutil cp %s gs://%s/weights' % (f2, opt.bucket)) if opt.bucket and ispt else None  # upload

    if not opt.evolve:
        plot_results()  # save as results.png
    print('%g epochs completed in %.3f hours.\n' % (epoch - start_epoch + 1, (time.time() - t0) / 3600))
    dist.destroy_process_group() if torch.cuda.device_count() > 1 else None
    torch.cuda.empty_cache()
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=250)  # 500200 batches at bs 16, 117263 COCO images = 273 epochs
    parser.add_argument('--batch-size', type=int, default=16)  # effective bs = batch_size * accumulate = 16 * 4 = 64
    parser.add_argument('--cfg', type=str, default='data_firev6/normal_prune_0.3_yolov3-tiny.cfg', help='*.cfg path')
    parser.add_argument('--t_cfg', type=str, default='', help='teacher model cfg file path for knowledge distillation')
    parser.add_argument('--data', type=str, default='data_firev6/yolo.data', help='*.data path')
    parser.add_argument('--multi-scale', action='store_true', help='adjust (67%% - 150%%) img_size every 10 batches')
    parser.add_argument('--img-size', nargs='+', type=int, default=[416, 416], help='[min_train, max-train, test]')
    parser.add_argument('--rect', action='store_true', help='rectangular training')
    parser.add_argument('--resume', action='store_true', help='resume training from v4_tiny_416_map61.pt')
    parser.add_argument('--nosave', action='store_true', help='only save final checkpoint')
    parser.add_argument('--notest', action='store_true', help='only test final epoch')
    parser.add_argument('--evolve', action='store_true', help='evolve hyperparameters')
    parser.add_argument('--bucket', type=str, default='', help='gsutil bucket')
    parser.add_argument('--cache-images', action='store_true', help='cache images for faster training')
    # parser.add_argument('--weights', type=str, default='data_firev6/backup/normal_prune_0.3_percent.weights', help='initial weights path')
    parser.add_argument('--weights', type=str, default='weights/DM_darnet.pt', help='initial weights path')
    parser.add_argument('--t_weights', type=str, default='', help='teacher model weights')
    parser.add_argument('--AT_str', type=int, default=-1, help='Attention tranfer  strategy')
    parser.add_argument('--KDstr', type=int, default=-1, help='KD strategy')
    parser.add_argument('--name', default='', help='renames results.txt to results_name.txt if supplied')
    parser.add_argument('--device', default='', help='device id (i.e. 0 or 0,1 or cpu)')
    parser.add_argument('--adam', action='store_true', help='use adam optimizer')
    parser.add_argument('--ema', action='store_true', help='use ema')
    parser.add_argument('--single-cls', action='store_true', help='train as single-class dataset')
    parser.add_argument('--sparsity-regularization', '-sr', dest='sr', action='store_true',
                        help='train with channel sparsity regularization')
    parser.add_argument('--pretrain', '-pt', dest='pt', action='store_true',
                        help='use pretrain model')
    parser.add_argument('--s', type=float, default=0.001, help='scale sparse rate')
    parser.add_argument('--prune', type=int, default=0,
                        help='0:nomal prune or regular prune 1:shortcut prune 2:layer prune')
    parser.add_argument('--quantized', type=int, default=0,
                        help='0:quantization way one Ternarized weight and 8bit activation')
    parser.add_argument('--a_bit', type=int, default=8,
                        help='a-bit')
    parser.add_argument('--w-bit', type=int, default=8,
                        help='w-bit')
    parser.add_argument('--BN_Fold', action='store_true', help='BN_Fold')
    parser.add_argument('--FPGA', action='store_true', help='FPGA')

    opt = parser.parse_args()
    opt.weights = last if opt.resume else opt.weights
    opt.cfg = list(glob.iglob('./**/' + opt.cfg, recursive=True))[0]  # find file
    # opt.data = list(glob.iglob(' ./**/' + opt.data, recursive=True))[0]  # find file
    print(opt)
    opt.img_size.extend([opt.img_size[-1]] * (3 - len(opt.img_size)))  # extend to 3 sizes (min, max, test)
    # device = torch_utils.select_device(opt.device, apex=mixed_precision, batch_size=opt.batch_size)
    device = select_device(opt.device, apex=mixed_precision, batch_size=opt.batch_size)

    if device.type == 'cpu':
        mixed_precision = False

    # scale hyp['obj'] by img_size (evolved at 320)
    # hyp['obj'] *= opt.img_size[0] / 320.

    tb_writer = None
    if not opt.evolve:  # Train normally
        print('Start Tensorboard with "tensorboard --logdir=runs", view at http://localhost:6006/')
        tb_writer = SummaryWriter(comment=opt.name)
        if opt.quantized != 0:
            times = 2 * math.log(32 / opt.a_bit, 2)
            print('<.....................using warm up.......................>')
            for i in range(0, int(times)):
                if i == 0:
                    a_bit = 32
                    w_bit = 32
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                elif i == 1:
                    a_bit = 16
                    w_bit = 32
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                elif i == 2:
                    a_bit = 16
                    w_bit = 16
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                elif i == 3:
                    a_bit = 8
                    w_bit = 16
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                elif i == 4:
                    a_bit = 8
                    w_bit = 8
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                elif i == 5:
                    a_bit = 4
                    w_bit = 8
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                elif i == 6:
                    a_bit = 4
                    w_bit = 4
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                elif i == 7:
                    a_bit = 2
                    w_bit = 4
                    print("Warm up by activation %d bits  and weight %d bit" % (a_bit, w_bit))
                    # WarmupForQ(hyp, step=i, a_bit=a_bit, w_bit=w_bit)
                else:
                    print("Quantization bits are limited 16, 8, 4, 2 !")
            # The learning rate decreases tenfold after quantification
            hyp['lr0'] = hyp['lr0'] * 0.1
        train(hyp)  # train normally

    else:  # Evolve hyperparameters (optional)
        opt.notest, opt.nosave = True, True  # only test/save final epoch
        if opt.bucket:
            os.system('gsutil cp gs://%s/evolve.txt .' % opt.bucket)  # download evolve.txt if exists

        for _ in range(1):  # generations to evolve
            if os.path.exists('evolve.txt'):  # if evolve.txt exists: select best hyps and mutate
                # Select parent(s)
                parent = 'single'  # parent selection method: 'single' or 'weighted'
                x = np.loadtxt('evolve.txt', ndmin=2)
                n = min(5, len(x))  # number of previous results to consider
                x = x[np.argsort(-fitness(x))][:n]  # top n mutations
                w = fitness(x) - fitness(x).min()  # weights
                if parent == 'single' or len(x) == 1:
                    # x = x[random.randint(0, n - 1)]  # random selection
                    x = x[random.choices(range(n), weights=w)[0]]  # weighted selection
                elif parent == 'weighted':
                    x = (x * w.reshape(n, 1)).sum(0) / w.sum()  # weighted combination

                # Mutate
                method, mp, s = 3, 0.9, 0.2  # method, mutation probability, sigma
                npr = np.random
                npr.seed(int(time.time()))
                g = np.array([1, 1, 1, 1, 1, 1, 1, 0, .1, 1, 0, 1, 1, 1, 1, 1, 1, 1])  # gains
                ng = len(g)
                if method == 1:
                    v = (npr.randn(ng) * npr.random() * g * s + 1) ** 2.0
                elif method == 2:
                    v = (npr.randn(ng) * npr.random(ng) * g * s + 1) ** 2.0
                elif method == 3:
                    v = np.ones(ng)
                    while all(v == 1):  # mutate until a change occurs (prevent duplicates)
                        # v = (g * (npr.random(ng) < mp) * npr.randn(ng) * s + 1) ** 2.0
                        v = (g * (npr.random(ng) < mp) * npr.randn(ng) * npr.random() * s + 1).clip(0.3, 3.0)
                for i, k in enumerate(hyp.keys()):  # plt.hist(v.ravel(), 300)
                    hyp[k] = x[i + 7] * v[i]  # mutate

            # Clip to limits
            keys = ['lr0', 'iou_t', 'momentum', 'weight_decay', 'hsv_s', 'hsv_v', 'translate', 'scale', 'fl_gamma']
            limits = [(1e-5, 1e-2), (0.00, 0.70), (0.60, 0.98), (0, 0.001), (0, .9), (0, .9), (0, .9), (0, .9), (0, 3)]
            for k, v in zip(keys, limits):
                hyp[k] = np.clip(hyp[k], v[0], v[1])

            # Train mutation
            results = train(hyp.copy())

            # Write mutation results
            print_mutation(hyp, results, opt.bucket)

            # Plot results
            # plot_evolution_results(hyp)

'''
for normal training:
python3 train.py --data data/voc.data --batch-size 26 -pt --weights weights/VOC-weight/0-normal-train/Jan-23-v4-normal-train-lr0-0.002/last.weights  --cfg cfg/yolov4/yolov4-voc.cfg --img-size 608  --epochs 5
python3 train.py --data data/voc.data --batch-size 16  --weights weights/VOC-weight/0-normal-train/Jan-23-v4-normal-train-lr0-0.002/last.weights  --cfg cfg/yolov4/yolov4-voc.cfg --img-size 416  --epochs 5

for sparse training:

python3 train.py --data data/voc.data --batch-size 32  --weights weights/yolov3_part/normal_train/v3_normal_map86.weights  --cfg cfg/yolov3/yolov3-voc.cfg   --img-size 416  --epochs 200 --device 0 -sr --s 0.005 --prune 1


for distilling  
python3 train.py --data data/voc.data --batch-size 156   --cfg cfg/yolov4tiny/yolov4-tiny_voc_group_map40.cfg  --weights weights/fine_tune_v4tiny_voc_map37.weights  --t_cfg cfg/yolov4tiny/yolov4-tiny_voc.cfg --t_weight weights/Lab_compression_dec19/voc/tiny/v4_tiny_416_voc_map70p9.weights --img-size 416  --epochs 20 --KDstr 2

'''