import matplotlib.pyplot as plt
import os
import argparse
import torchvision
import torch.optim as optim
from torchvision import transforms
import datetime
from models import *
from earlystop import earlystop
import numpy as np
import attack_generator as attack
from utils import Logger
from torchvision.datasets import STL10
from torch.utils.data import DataLoader

parser = argparse.ArgumentParser(description='PyTorch Friendly Adversarial Training for MART')
parser.add_argument('--epochs', type=int, default=120, metavar='N', help='number of epochs to train')
parser.add_argument('--weight_decay', '--wd', default=2e-4, type=float, metavar='W')
parser.add_argument('--lr', type=float, default=0.1, metavar='LR', help='learning rate')
parser.add_argument('--momentum', type=float, default=0.9, metavar='M', help='SGD momentum')
parser.add_argument('--epsilon', type=float, default=0.031, help='perturbation bound')
parser.add_argument('--num_steps', type=int, default=10, help='maximum perturbation step K')
parser.add_argument('--step_size', type=float, default=0.007, help='step size')
parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed')
parser.add_argument('--net', type=str, default="WRN",help="decide which network to use,choose from smallcnn,resnet18,WRN")
parser.add_argument('--tau', type=int, default=0, help='step tau')
parser.add_argument('--beta',type=float,default=6.0,help='regularization parameter')
parser.add_argument('--dataset', type=str, default="cifar10", help="choose from cifar10,svhn caltech101")
parser.add_argument('--rand_init', type=bool, default=True, help="whether to initialize adversarial sample with random noise")
parser.add_argument('--omega', type=float, default=0.0, help="random sample parameter")
parser.add_argument('--dynamictau', type=bool, default=True, help='whether to use dynamic tau')
parser.add_argument('--depth', type=int, default=34, help='WRN depth')
parser.add_argument('--width_factor', type=int, default=10, help='WRN width factor')
parser.add_argument('--drop_rate', type=float, default=0.0, help='WRN drop rate')
parser.add_argument('--out_dir',type=str,default='./FAT_for_MART_results',help='dir of output')
parser.add_argument('--resume', type=str, default='', help='whether to resume training, default: None')

args = parser.parse_args()

# settings
torch.manual_seed(args.seed)
np.random.seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
torch.backends.cudnn.deterministic = False
torch.backends.cudnn.benchmark = True

out_dir = args.out_dir
if not os.path.exists(out_dir):
    os.makedirs(out_dir)

def MART_loss(adv_logits, natural_logits, target, beta):
    # Based on the repo MART https://github.com/YisenWang/MART
    kl = nn.KLDivLoss(reduction='none')
    batch_size = len(target)
    adv_probs = F.softmax(adv_logits, dim=1)
    tmp1 = torch.argsort(adv_probs, dim=1)[:, -2:]
    new_y = torch.where(tmp1[:, -1] == target, tmp1[:, -2], tmp1[:, -1])
    loss_adv = F.cross_entropy(adv_logits, target) + F.nll_loss(torch.log(1.0001 - adv_probs + 1e-12), new_y)
    nat_probs = F.softmax(natural_logits, dim=1)
    true_probs = torch.gather(nat_probs, 1, (target.unsqueeze(1)).long()).squeeze()
    loss_robust = (1.0 / batch_size) * torch.sum(
        torch.sum(kl(torch.log(adv_probs + 1e-12), nat_probs), dim=1) * (1.0000001 - true_probs))
    loss = loss_adv + float(beta) * loss_robust
    return loss

def train(model, train_loader, optimizer, tau):
    starttime = datetime.datetime.now()
    loss_sum = 0
    bp_count = 0
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.cuda(), target.cuda()

        # Get friendly adversarial training data via early-stopped PGD
        output_adv, output_target, output_natural, count = earlystop(model, data, target, step_size=args.step_size,
                                                                     epsilon=args.epsilon, perturb_steps=args.num_steps,
                                                                     tau=tau, randominit_type="normal_distribution_randominit", loss_fn='cent', rand_init=args.rand_init,
                                                                     omega=args.omega)
        bp_count += count
        model.train()
        optimizer.zero_grad()

        adv_logits = model(output_adv)
        natural_logits = model(output_natural)

        # calculate MART adversarial training loss
        loss = MART_loss(adv_logits, natural_logits, output_target, args.beta)

        loss_sum += loss.item()
        loss.backward()
        optimizer.step()

    bp_count_avg = bp_count / len(train_loader.dataset)
    endtime = datetime.datetime.now()
    time = (endtime - starttime).seconds

    return time, loss_sum, bp_count_avg

def adjust_tau(epoch, dynamictau):
    tau = args.tau
    if dynamictau:
        if epoch <= 20:
            tau = 0
        elif epoch <= 40:
            tau = 1
        elif epoch <= 60:
            tau = 2
        elif epoch <= 80:
            tau = 3
        else:
            tau = 4
    return tau

def adjust_learning_rate(optimizer, epoch):
    """decrease the learning rate"""
    lr = args.lr
    if epoch >= 60:
        lr = args.lr * 0.1
    if epoch >= 90:
        lr = args.lr * 0.01
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

def save_checkpoint(state, checkpoint=out_dir, filename='checkpoint.pth.tar'):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)

# setup data loader
if args.dataset == "STL10":
    transform_train = transforms.Compose([
        transforms.Resize((32, 32)),  # Resize images to 32x32 pixels
        transforms.ToTensor(),         # Convert images to tensors (0-1 range)
        transforms.Normalize(          # Normalize the images (mean and std)
        mean=[0.5, 0.5, 0.5],          # These values are precomputed for STL-10
        std=[0.5, 0.5, 0.5]
    )
    ])
    transform_test = transform_train
else:
    # setup data loader
    transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
    ])
    transform_test = transforms.Compose([
        transforms.ToTensor(),
    ])

print('==> Load Test Data')
if args.dataset == "cifar10":
    trainset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
    train_loader = torch.utils.data.DataLoader(trainset, batch_size=128, shuffle=True, num_workers=2)
    testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
    test_loader = torch.utils.data.DataLoader(testset, batch_size=128, shuffle=False, num_workers=2)
if args.dataset == "svhn":
    trainset = torchvision.datasets.SVHN(root='./data', split='train', download=True, transform=transform_train)
    train_loader = torch.utils.data.DataLoader(trainset, batch_size=128, shuffle=True, num_workers=2)
    testset = torchvision.datasets.SVHN(root='./data', split='test', download=True, transform=transform_test)
    test_loader = torch.utils.data.DataLoader(testset, batch_size=128, shuffle=False, num_workers=2)

if args.dataset == "caltech101":
    trainset = torchvision.datasets.Caltech101(root='./data', download=True, transform=transform_train)
    train_loader = torch.utils.data.DataLoader(trainset, batch_size=128, shuffle=True, num_workers=2)
    testset = torchvision.datasets.Caltech101(root='./data', download=True, transform=transform_test) 
    test_loader = torch.utils.data.DataLoader(testset, batch_size=128, shuffle=False, num_workers=2)

if args.dataset == "STL10":
    # Load the STL-10 dataset (specify root directory where data is located)
    train_dataset = STL10(root='./data', split='train', transform=transform_train, download=True)
    test_dataset  = STL10(root='./data', split='test', transform=transform_test, download=True)
    # Create DataLoader objects for training and testing
    train_loader = torch.utils.data.DataLoader(train_dataset, 128, shuffle=True, num_workers=2)
    test_loader = torch.utils.data.DataLoader(test_dataset, 128, shuffle=False, num_workers=2)

print('==> Load Model')
if args.net == "smallcnn":
    model = SmallCNN().cuda()
    net = "smallcnn"
if args.net == "resnet18":
    model = ResNet18().cuda()
    net = "resnet18"
if args.net == "WRN":
    model = Wide_ResNet(depth=args.depth, num_classes=10, widen_factor=args.width_factor, dropRate=args.drop_rate).cuda()
    net = "WRN{}-{}-dropout{}".format(args.depth, args.width_factor, args.drop_rate)
model = torch.nn.DataParallel(model)
print(net)

optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

if not os.path.exists(out_dir):
    os.makedirs(out_dir)

start_epoch = 0
# Resume
title = 'FAT for MART train'
if args.resume:
    # resume directly point to checkpoint.pth.tar e.g., --resume='./out-dir/checkpoint.pth.tar'
    print ('==> Friendly Adversarial Training for MART Resuming from checkpoint ..')
    print(args.resume)
    assert os.path.isfile(args.resume)
    out_dir = os.path.dirname(args.resume)
    checkpoint = torch.load(args.resume)
    start_epoch = checkpoint['epoch']
    model.load_state_dict(checkpoint['state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    logger_test = Logger(os.path.join(out_dir, 'log_results.txt'), title=title, resume=True)
else:
    print('==> Friendly Adversarial Training for MART')
    logger_test = Logger(os.path.join(out_dir, 'log_results.txt'), title=title)
    logger_test.set_names(['Epoch', 'Natural Test Acc', 'FGSM Acc', 'PGD20 Acc', 'CW Acc'])


test_nat_acc = 0
fgsm_acc = 0
test_pgd20_acc = 0
cw_acc = 0
for epoch in range(start_epoch, args.epochs):
    adjust_learning_rate(optimizer, epoch + 1)
    train_time, train_loss, bp_count_avg = train(model, train_loader, optimizer, adjust_tau(epoch + 1, args.dynamictau))

    ## Evalutions the same as TRADES.
    loss, test_nat_acc = attack.eval_clean(model, test_loader)
    loss, fgsm_acc = attack.eval_robust(model, test_loader, perturb_steps=1, epsilon=0.031, step_size=0.031,loss_fn="cent", category="Madry",rand_init=True)
    loss, test_pgd20_acc = attack.eval_robust(model,test_loader, perturb_steps=20, epsilon=0.031, step_size=0.003,loss_fn="cent",category="Madry",rand_init=True)
    loss, cw_acc = attack.eval_robust(model,test_loader, perturb_steps=30, epsilon=0.031, step_size=0.003,loss_fn="cw",category="Madry",rand_init=True)

    print(
        'Epoch: [%d | %d] | Train Time: %.2f s | BP Average: %.2f | Natural Test Acc %.2f | FGSM Test Acc %.2f | PGD20 Test Acc %.2f | CW Test Acc %.2f |\n' % (
            epoch + 1,
            args.epochs,
            train_time,
            bp_count_avg,
            test_nat_acc,
            fgsm_acc,
            test_pgd20_acc,
            cw_acc)
    )

    logger_test.append([epoch + 1, test_nat_acc, fgsm_acc, test_pgd20_acc, cw_acc])

    save_checkpoint({
        'epoch': epoch + 1,
        'state_dict': model.state_dict(),
        'bp_avg': bp_count_avg,
        'test_nat_acc': test_nat_acc,
        'test_pgd20_acc': test_pgd20_acc,
        'optimizer': optimizer.state_dict(),
    })
logger_test.plot()
plt.show()