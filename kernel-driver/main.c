// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause

#include <linux/module.h>
#include <linux/etherdevice.h>
#include <linux/netdevice.h>

#include "bluerdma.h"
#include "verbs.h"
#include "ethernet.h"

MODULE_AUTHOR("Hange Shen <Foreverhighness@gmail.com>");
MODULE_DESCRIPTION("DatenLord RDMA adapter driver");
MODULE_LICENSE("Dual BSD/GPL");

#pragma region ib device

#define N_TESTING 4
static struct bluerdma_dev *testing_dev[N_TESTING] = {};

static int bluerdma_new_testing(void)
{
	struct bluerdma_dev *dev;
	int i, ret;

	for (i = 0; i < N_TESTING; i++) {
		dev = ib_alloc_device(bluerdma_dev, ibdev);
		if (!dev) {
			pr_err("ib_alloc_device failed for index %d\n", i);
			while (--i >= 0) {
				if (testing_dev[i]->netdev)
					bluerdma_destroy_netdev(testing_dev[i]);
				ib_dealloc_device(&testing_dev[i]->ibdev);
				testing_dev[i] = NULL;
			}
			return -ENOMEM;
		}
		testing_dev[i] = dev;
		dev->id = i;
		pr_info("ib_alloc_device ok for index %d\n", dev->id);

		ret = bluerdma_create_netdev(dev, i);
		if (ret) {
			pr_err("bluerdma_create_netdev failed for index %d\n",
			       i);
			ib_dealloc_device(&dev->ibdev);
			while (--i >= 0) {
				if (testing_dev[i]->netdev)
					bluerdma_destroy_netdev(testing_dev[i]);
				ib_dealloc_device(&testing_dev[i]->ibdev);
				testing_dev[i] = NULL;
			}
			return ret;
		}
	}

	return 0;
}

static void bluerdma_free_testing(void)
{
	int i;
	for (i = 0; i < N_TESTING; i++) {
		if (testing_dev[i]) {
			if (testing_dev[i]->netdev)
				bluerdma_destroy_netdev(testing_dev[i]);
			ib_dealloc_device(&testing_dev[i]->ibdev);
			testing_dev[i] = NULL;
			pr_info("ib_dealloc_device ok for index %d\n", i);
		}
	}
}

static const struct ib_device_ops bluerdma_device_ops = {
	.owner = THIS_MODULE,
	.driver_id = RDMA_DRIVER_UNKNOWN,
	.uverbs_abi_ver = 1,

	// mandatory methods <https://elixir.bootlin.com/linux/v6.8/source/drivers/infiniband/core/device.c#L267>
	.query_device = bluerdma_query_device,
	.query_port = bluerdma_query_port,

	.alloc_pd = bluerdma_alloc_pd,
	.dealloc_pd = bluerdma_dealloc_pd,

	.create_qp = bluerdma_create_qp,
	.modify_qp = bluerdma_modify_qp,
	.destroy_qp = bluerdma_destroy_qp,

	.post_send = bluerdma_post_send,
	.post_recv = bluerdma_post_recv,

	.create_cq = bluerdma_create_cq,
	.destroy_cq = bluerdma_destroy_cq,
	.poll_cq = bluerdma_poll_cq,

	.req_notify_cq = bluerdma_req_notify_cq,

	.get_dma_mr = bluerdma_get_dma_mr,
	.reg_user_mr = bluerdma_reg_user_mr,
	.dereg_mr = bluerdma_dereg_mr,

	.get_port_immutable = bluerdma_get_port_immutable,
	// optional methods

	// uverbs required methods
	.alloc_ucontext = bluerdma_alloc_ucontext,
	.dealloc_ucontext = bluerdma_dealloc_ucontext,

	.query_gid = bluerdma_query_gid,
	.query_pkey = bluerdma_query_pkey,
	.add_gid = bluerdma_add_gid,
	.del_gid = bluerdma_del_gid,

	// init size
	// INIT_RDMA_OBJ_SIZE(ib_ah, bluerdma_ah, ibah),
	INIT_RDMA_OBJ_SIZE(ib_cq, bluerdma_cq, ibcq),
	INIT_RDMA_OBJ_SIZE(ib_pd, bluerdma_pd, ibpd),
	INIT_RDMA_OBJ_SIZE(ib_qp, bluerdma_qp, ibqp),
	INIT_RDMA_OBJ_SIZE(ib_ucontext, bluerdma_ucontext, ibuc),
	// INIT_RDMA_OBJ_SIZE(ib_srq, bluerdma_srq, ibsrq),
	// INIT_RDMA_OBJ_SIZE(ib_mw, bluerdma_mw, ibmw),
};

static int bluerdma_ib_device_add(struct pci_dev *pdev)
{
	struct ib_device *ibdev;
	int ret, i;

	ret = bluerdma_new_testing();
	if (ret) {
		pr_err("bluerdma_new_testing failed\n");
		return ret;
	}

	for (i = 0; i < N_TESTING; i++) {
		ibdev = &testing_dev[i]->ibdev;

		strscpy(ibdev->node_desc, "bluerdma", sizeof(ibdev->node_desc));

		ibdev->node_type = RDMA_NODE_RNIC;
		ibdev->phys_port_cnt = 1;
		ibdev->num_comp_vectors = num_possible_cpus();
		ibdev->local_dma_lkey = 0;

		ib_set_device_ops(ibdev, &bluerdma_device_ops);
		pr_info("ib_set_device_ops ok for index %d\n", i);

		bluerdma_init_sysfs_attrs(testing_dev[i]);

		ret = ib_register_device(ibdev, "bluerdma%d", NULL);
		if (ret) {
			pr_err("ib_register_device failed for index %d\n", i);
			while (--i >= 0) {
				ib_unregister_device(&testing_dev[i]->ibdev);
			}
			bluerdma_free_testing();
			return ret;
		}
		pr_info("ib_register_device %s\n", ibdev->name);

		ret = device_create_file(&ibdev->dev,
					 &testing_dev[i]->gids_attr);
		if (ret) {
			pr_err("Failed to create gids sysfs file for device %d\n",
			       i);
		}

		ret = device_create_file(&ibdev->dev,
					 &testing_dev[i]->mac_attr);
		if (ret) {
			pr_err("Failed to create mac sysfs file for device %d\n",
			       i);
		}

		if (testing_dev[i]->netdev) {
			ret = ib_device_set_netdev(ibdev,
						   testing_dev[i]->netdev, 1);
			if (ret) {
				pr_err("ib_device_set_netdev failed for index %d: %d\n",
				       i, ret);
			} else {
				pr_info("Associated netdev %s with RDMA device %s\n",
					testing_dev[i]->netdev->name,
					ibdev->name);
			}
		}
	}

	return 0;
}

static void bluerdma_ib_device_remove(struct pci_dev *pdev)
{
	// struct bluerdma_dev *dev = pci_get_drvdata(pdev);
	for (int i = 0; i < N_TESTING; i++) {
		if (testing_dev[i]) {
			device_remove_file(&testing_dev[i]->ibdev.dev,
					   &testing_dev[i]->gids_attr);
			device_remove_file(&testing_dev[i]->ibdev.dev,
					   &testing_dev[i]->mac_attr);

			ib_unregister_device(&testing_dev[i]->ibdev);
			pr_info("ib_unregister_device ok for index %d\n", i);
		}
	}
	bluerdma_free_testing();
}

#pragma endregion ib device

#pragma region probe

static int bluerdma_probe(struct pci_dev *pdev, const struct pci_device_id *ent)
{
	int ret;

	// ret = bluerdma_probe_dev(pdev);
	// pr_info("bluerdma_probe_dev ok %d\n", ret == 0);
	// if (ret)
	// 	return ret;

	ret = bluerdma_ib_device_add(pdev);
	pr_info("bluerdma_ib_device_add %d\n", ret == 0);
	if (ret) {
		// bluerdma_remove_dev(pdev);
		return ret;
	}

	return 0;
}

static void bluerdma_remove(struct pci_dev *pdev)
{
	bluerdma_ib_device_remove(pdev);
	// bluerdma_remove_dev(pdev);
}

#pragma endregion probe

#pragma region PCI Device

// static struct pci_driver bluerdma_pci_driver = {
// 	.name = DRV_MODULE_NAME,
// 	.id_table = bluerdma_pci_tbl,
// 	.probe = bluerdma_probe,
// 	.remove = bluerdma_remove
// };
// static const struct pci_device_id bluerdma_pci_tbl[] = {
// TODO: Add the correct PCI device ID
// 	{ PCI_DEVICE(PCI_VENDOR_ID_DATENLORD, 0xffff) },
// 	{}
// };
// MODULE_DEVICE_TABLE(pci, bluerdma_pci_tbl);

#pragma endregion PCI Device

#pragma region Entry Point

static int __init bluerdma_init_module(void)
{
	pr_info("DatenLord RDMA driver loaded\n");
	int ret;

	ret = request_module("ib_uverbs");
	// ret = pci_register_driver(&bluerdma_pci_driver);
	ret = bluerdma_probe(NULL, NULL);

	return 0;
}

static void __exit bluerdma_exit_module(void)
{
	pr_info("DatenLord RDMA driver unloaded\n");

	bluerdma_remove(NULL);
	// pci_unregister_driver(&bluerdma_pci_driver);
}

module_init(bluerdma_init_module);
module_exit(bluerdma_exit_module);

#pragma endregion Entry Point
