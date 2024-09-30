


python3 train.py --data darknet_DM.data --batch-size 32 --accumulate 1 --weights darknet_DM_best.weights --cfg darknet_DM.cfg -sr --s 0.001 --prune 0 --device 0


python3 normal_prune.py --cfg DM/darknet_DM.cfg --data DM/darknet_DM.data --weights weights/best.pt