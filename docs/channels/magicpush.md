# 魔法推送

[← 推送渠道总览](../notification-channels.md) · [← README](../../README.md)

## 一、安装魔法推送

安装方式一：从飞牛应用商店安装

![image-20260408111555952](images/image-20260408111555952.png)

安装方式二：通过docker compose方式安装

```yml
services:
  magicpush:
    image: docker.cnb.cool/magiccode1412/magicpush:latest
    ports:
      - "818:3000"
    volumes:
      - ./data:/app/server/data
    network_mode: bridge
    container_name: magicpush
```

## 二、配置魔法推送

通过`飞牛控制台桌面图标`或者`nasip+端口`方式打开魔法推送

1. 注册

    首次注册，默认为管理员，之后用户注册功能默认关闭，有需要可以在设置页面手动打开

    ![image-20260408112320034](images/image-20260408112320034.png)

2. 添加推送渠道

    切换到`渠道推送`页面，添加一个或者多个渠道

    ![image-20260408112914367](images/image-20260408112914367.png)

3. 添加接口

    切换到`接口管理`页面，添加一个接口，一个接口可以同时绑定多个渠道，会得到一个`访问令牌`

    ![image-20260408113237867](images/image-20260408113237867.png)

4. 记录魔法推送的必要的信息
    + 基础 URL：服务根地址，**不要**带 `/api/push`。例如局域网 `http://10.10.10.15:818`，或已反代的 `https://magicpush.example.com`
    + 访问令牌（token）：接口管理里该推送接口的令牌（不是登录用的 JWT）

日志推送按官方「方式一」调用：`POST {基础URL}/api/push/{token}`，JSON 体含 `title` / `content` / `type`（避免经飞牛反代域名时 Authorization 被拦成 403）。

> 推荐：日志推送与魔法推送在同一台 NAS 时，基础 URL 填 **局域网 IP:端口**（如 `http://192.168.1.10:818`），不要填 `https://xxx.fnos.net` 反代域名——容器从内网打公网域名回环，反代常直接返回 403。

## 三、配置日志推送

这里默认你已经安装好`日志推送`

1. 滚动到`推送渠道`，选择`魔法推送`

    ![image-20260408114210538](images/image-20260408114210538.png)

2. 把上面获取到的`基础 URL`和`token`填入对应的位置，滑动到保存按钮，保存配置

    ![image-20260408114513309](images/image-20260408114513309.png)

    ![image-20260408114649782](images/image-20260408114649782.png)

3. 测试

    滚动到最下面的`测试推送`，随便输入点内容，点击发送测试

    ![image-20260408114828845](images/image-20260408114828845.png)

    ![image-20260408114857330](images/image-20260408114857330.png)

4. 至此，配置已完成

## 其他问题

1. 以上教程基于日志推送和魔法推送部署在同一个局域网，如果不在同一个局域网，魔法推送需要自行配置域名（注意 ip / https 证书，容器内也要能解析并访问该域名）
2. 基础 URL 勿填 `localhost` / `127.0.0.1`（日志推送在 Docker 内时指向的是自己）；勿在基础 URL 末尾再写 `/api/push`
3. 若日志出现 `HTTP 403` 且基础 URL 是 `*.fnos.net` 反代域名：请改为魔法推送映射端口的局域网地址（同 NAS 容器经反代回环常被拦）
4. 测试失败若提示 `HTTP 400: 部分推送失败`，说明日志推送已打到魔法推送，但魔法推送绑定的下游渠道失败，需在魔法推送后台检查渠道
5. 如果有其他问题，可以在日志推送的微信群请教或者[点此提issue](https://github.com/magiccode1412/magicpush/issues)