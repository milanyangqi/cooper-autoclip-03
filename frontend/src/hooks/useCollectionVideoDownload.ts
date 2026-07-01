import { useState } from 'react'
import { message } from 'antd'
import { projectApi } from '../services/api'

const DOWNLOAD_CHAIN_DELAY_MS = 800
const waitForNextDownload = () => new Promise((resolve) => window.setTimeout(resolve, DOWNLOAD_CHAIN_DELAY_MS))
const getDownloadErrorMessage = (error: any, fallback: string) => error?.userMessage || fallback

export const useCollectionVideoDownload = () => {
  const [isGenerating, setIsGenerating] = useState(false)

  const generateAndDownloadCollectionVideo = async (
    projectId: string, 
    collectionId: string,
    _collectionTitle: string,
    asset: 'video' | 'subtitle' | 'both' = 'video'
  ) => {
    if (isGenerating) return

    setIsGenerating(true)
    
    try {
      if (asset === 'subtitle') {
        await projectApi.downloadVideo(projectId, undefined, collectionId, 'subtitle')
        message.success('合集字幕下载完成')
        return
      }

      // 直接按用户当前调整的顺序生成合集视频，并同步更新合集字幕。
      message.info('正在按您的顺序生成合集视频...')
      await projectApi.generateCollectionVideo(projectId, collectionId)
      message.success('合集视频生成成功，正在下载...')

      await new Promise((resolve) => setTimeout(resolve, 1000))
      await projectApi.downloadVideo(projectId, undefined, collectionId, 'video')
      message.success('合集视频下载完成')

      if (asset === 'both') {
        await waitForNextDownload()
        await projectApi.downloadVideo(projectId, undefined, collectionId, 'subtitle')
        message.success('合集字幕下载完成')
      }
      
    } catch (error) {
      console.error('生成合集视频失败:', error)
      message.error(getDownloadErrorMessage(error, asset === 'subtitle' ? '合集字幕下载失败' : '合集视频下载失败'))
    } finally {
      setIsGenerating(false)
    }
  }

  return {
    isGenerating,
    generateAndDownloadCollectionVideo
  }
}
